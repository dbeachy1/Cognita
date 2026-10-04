{ ---------------------------------------------------------------------------------------------
  The PURE block of Cognita.iss, moved here by design 19.10 so a harness can compile it on its own:
  windows\setup\tests\pure_tests.iss #includes this file, runs a list of cases at InitializeSetup and
  writes PASS/FAIL lines to a file, and tests\test_setup_pure.py compiles and runs it with ISCC.
  Cognita.iss #includes it inside its [Code] section, where it used to be inline.

  Rules for this file: no wizard objects, no globals of Cognita.iss, no helper runs, no file or registry
  access. Anything here must run the same in Setup and in the harness, because that is what the tests
  prove. (Only Inno's own support functions such as Copy, Pos, Trim, RemoveBackslash and StrToIntDef
  are used.)
  --------------------------------------------------------------------------------------------- }

{ ==== PURE-BEGIN (no wizard objects below this line until PURE-END; tested by a harness) ==== }

function IsWs(const C: Char): Boolean;
begin
  Result := (C = ' ') or (C = #9) or (C = #10) or (C = #13);
end;

function HexDigit(N: Integer): String;
begin
  if N < 10 then
    Result := Chr(Ord('0') + N)
  else
    Result := Chr(Ord('a') + N - 10);
end;

function HexOf(V: Cardinal): String;
var
  I: Integer;
begin
  Result := '';
  for I := 7 downto 0 do
    Result := Result + HexDigit(Integer((V shr (I * 4)) and $F));
end;

{ Reads the JSON string whose opening quote is at S[I]. Decodes the escapes the helper can emit.
  On return I is just past the closing quote. }
function JsonReadString(const S: String; var I: Integer): String;
var
  N, Code: Integer;
  C: Char;
begin
  Result := '';
  N := Length(S);
  I := I + 1;
  while I <= N do
  begin
    C := S[I];
    if C = '"' then
    begin
      I := I + 1;
      Exit;
    end;
    if (C = '\') and (I < N) then
    begin
      I := I + 1;
      C := S[I];
      case C of
        'n': Result := Result + #13#10;
        'r': ;
        't': Result := Result + ' ';
        'u':
          begin
            Code := StrToIntDef('$' + Copy(S, I + 1, 4), 63);
            Result := Result + Chr(Code);
            I := I + 4;
          end;
      else
        Result := Result + C;
      end;
    end
    else
      Result := Result + C;
    I := I + 1;
  end;
end;

{ The value of one key of a flat JSON object (one progress line). Strings come back decoded,
  numbers and words come back as their text. '' when the key is absent. No JSON library: the
  object is walked pair by pair, so a key name inside a string value cannot match. }
function JsonField(const Line, Key: String): String;
var
  I, N, Start: Integer;
  K, V: String;
begin
  Result := '';
  N := Length(Line);
  I := 1;
  while (I <= N) and (Line[I] <> '{') do
    I := I + 1;
  I := I + 1;
  while I <= N do
  begin
    while (I <= N) and (IsWs(Line[I]) or (Line[I] = ',')) do
      I := I + 1;
    if (I > N) or (Line[I] = '}') then
      Exit;
    if Line[I] <> '"' then
      Exit;
    K := JsonReadString(Line, I);
    while (I <= N) and IsWs(Line[I]) do
      I := I + 1;
    if (I > N) or (Line[I] <> ':') then
      Exit;
    I := I + 1;
    while (I <= N) and IsWs(Line[I]) do
      I := I + 1;
    if I > N then
      Exit;
    if Line[I] = '"' then
      V := JsonReadString(Line, I)
    else
    begin
      Start := I;
      while (I <= N) and (Line[I] <> ',') and (Line[I] <> '}') do
        I := I + 1;
      V := Trim(Copy(Line, Start, I - Start));
    end;
    if K = Key then
    begin
      Result := V;
      Exit;
    end;
  end;
end;

{ Decodes the two escapes the helper puts in result values (design 18.5): %3B is ';' and %25 is
  '%'. One left-to-right pass, so '%253B' is the text '%3B', not ';'. Any other '%' stays as it is.
  Hex digits are accepted in either case. }
function DecodeResultValue(const S: String): String;
var
  I, N: Integer;
  Code: String;
begin
  Result := '';
  N := Length(S);
  I := 1;
  while I <= N do
  begin
    if (S[I] = '%') and (I + 2 <= N) then
    begin
      Code := Uppercase(Copy(S, I + 1, 2));
      if Code = '3B' then
      begin
        Result := Result + ';';
        I := I + 3;
      end
      else if Code = '25' then
      begin
        Result := Result + '%';
        I := I + 3;
      end
      else
      begin
        Result := Result + S[I];
        I := I + 1;
      end;
    end
    else
    begin
      Result := Result + S[I];
      I := I + 1;
    end;
  end;
end;

{ One value from a `result=ok;key=value;key=value` line. '' when absent. The first field is the
  status itself, read with Key = 'result'. Every value goes through DecodeResultValue here, the one
  place values are read, so no caller ever sees %3B or %25 (design 18.5). }
function ResultValue(const ResultLine, Key: String): String;
var
  Hay, Needle: String;
  P, E: Integer;
begin
  Result := '';
  Hay := ';' + ResultLine + ';';
  Needle := ';' + Key + '=';
  P := Pos(Needle, Hay);
  if P = 0 then
    Exit;
  P := P + Length(Needle);
  E := P;
  while (E <= Length(Hay)) and (Hay[E] <> ';') do
    E := E + 1;
  Result := DecodeResultValue(Copy(Hay, P, E - P));
end;

{ The first http:// or https:// address in a progress message, up to the next space or line break,
  without the sentence punctuation that may follow it. '' when there is none. A link in the LATEST
  message stays clickable (design 18.4: the helper repeats the sign-in address in every heartbeat). }
function UrlInText(const Msg: String): String;
var
  Lower: String;
  P1, P2, P, E: Integer;
begin
  Result := '';
  Lower := Lowercase(Msg);
  P1 := Pos('https://', Lower);
  P2 := Pos('http://', Lower);
  if (P1 = 0) or ((P2 > 0) and (P2 < P1)) then
    P := P2
  else
    P := P1;
  if P = 0 then
    Exit;
  E := P;
  while (E <= Length(Msg)) and (Msg[E] <> ' ') and (Msg[E] <> #13) and (Msg[E] <> #10) and (Msg[E] <> #9) and
    (Msg[E] <> '"') and (Msg[E] <> '<') and (Msg[E] <> '>') do
    E := E + 1;
  Result := Copy(Msg, P, E - P);
  while (Length(Result) > 0) and ((Result[Length(Result)] = '.') or (Result[Length(Result)] = ',') or
    (Result[Length(Result)] = ')') or (Result[Length(Result)] = ';') or (Result[Length(Result)] = ':')) do
    Result := Copy(Result, 1, Length(Result) - 1);
  if (Lowercase(Result) = 'http://') or (Lowercase(Result) = 'https://') then
    Result := '';
end;

const
  ModeFresh = 0;                  { no owned distro }
  ModeRepair = 1;                 { installed, this Setup's version }
  ModeUpdate = 2;                 { installed, a different version, and not uninstalled }
  ModeFinish = 3;                 { owned distro but the Linux install never completed }
  ModeReinstall = 4;              { after a keep-data uninstall: installed=1 AND state=uninstalled }

{ The mode table of design 18.2, from what `state` said. DistroOwned is `distro=present` (a distro
  that is not ours, owned=0, is folded in by the caller as "not owned"); Installed is `installed=1`,
  meaning the distro is ours AND the Linux install completed; StateName is the `state` key; LinuxVersion
  is `linux_version` (the Setup version that last finished cognita install or update). After a
  keep-data uninstall the helper reports installed=1 AND state=uninstalled: the Linux side removed its
  releases, so `update` would be refused and the run is a reinstall over the kept data, whatever the
  version. An installed machine whose recorded version is empty or different is an update, never a
  repair. }
function PickMode(const DistroOwned, Installed: Boolean; const StateName, LinuxVersion, SetupVersion: String): Integer;
begin
  if not DistroOwned then
    Result := ModeFresh
  else if not Installed then
    Result := ModeFinish
  else if Lowercase(StateName) = 'uninstalled' then
    Result := ModeReinstall
  else if LinuxVersion = SetupVersion then
    Result := ModeRepair
  else
    Result := ModeUpdate;
end;

function IsTruthy(const V: String): Boolean;
var
  L: String;
begin
  L := Lowercase(Trim(V));
  Result := (L = '1') or (L = 'true') or (L = 'yes') or (L = 'on');
end;

{ distro=present says a distro named Cognita exists; owned=0 says it is not ours (check then fails
  with a name-clash message). Only an owned distro moves Setup out of the fresh mode. }
function StateDistroOwned(const StateLine: String): Boolean;
begin
  Result := (Lowercase(ResultValue(StateLine, 'distro')) = 'present') and (ResultValue(StateLine, 'owned') <> '0');
end;

{ The mode straight from a `state` result line (what InitializeSetup uses). }
function ModeFromState(const StateLine, SetupVersion: String): Integer;
begin
  Result := PickMode(StateDistroOwned(StateLine), IsTruthy(ResultValue(StateLine, 'installed')),
    ResultValue(StateLine, 'state'), ResultValue(StateLine, 'linux_version'), SetupVersion);
end;

{ Removes what follows /mcp/ up to the next separator: connector tokens ride in that path
  segment and must never reach a log (CLAUDE.md security invariants). }
function RedactToken(const S: String): String;
var
  Lower: String;
  P, E, N: Integer;
begin
  Result := S;
  Lower := Lowercase(S);
  P := Pos('/mcp/', Lower);
  if P = 0 then
    Exit;
  N := Length(S);
  P := P + 5;
  E := P;
  while (E <= N) and (S[E] <> '/') and (S[E] <> '"') and (S[E] <> '''') and (S[E] <> ' ')
    and (S[E] <> ';') and (S[E] <> '&') and (S[E] <> '?') and (S[E] <> ',') and (S[E] <> #13) do
    E := E + 1;
  if E > P then
    Result := Copy(S, 1, P - 1) + '<redacted>' + RedactToken(Copy(S, E, N - E + 1));
end;

{ Decimal size text: 512 MB, 1.4 GB. }
function FormatBytes(const B: Int64): String;
var
  Tenths: Int64;
begin
  if B >= 1000000000 then
  begin
    Tenths := (B + 50000000) div 100000000;
    Result := IntToStr(Tenths div 10) + '.' + IntToStr(Tenths mod 10) + ' GB';
  end
  else
    Result := IntToStr((B + 500000) div 1000000) + ' MB';
end;

function FormatElapsed(const Ms: Int64): String;
var
  S: Int64;
begin
  S := Ms div 1000;
  Result := IntToStr(S div 60) + ':';
  if (S mod 60) < 10 then
    Result := Result + '0';
  Result := Result + IntToStr(S mod 60);
end;

{ Quotes one command-line argument by the Windows rules (backslashes before a quote double up). }
function QuoteArg(const S: String): String;
var
  I, Trailing: Integer;
begin
  Trailing := 0;
  I := Length(S);
  while (I >= 1) and (S[I] = '\') do
  begin
    Trailing := Trailing + 1;
    I := I - 1;
  end;
  Result := '"' + S;
  for I := 1 to Trailing do
    Result := Result + '\';
  Result := Result + '"';
end;

function HasBadArgChar(const S: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to Length(S) do
    if (S[I] = '"') or (S[I] < ' ') then
      Result := True;
end;

function PartsEqual(const A, B: String): Boolean;
var
  X, Y: String;
begin
  X := Lowercase(Trim(A));
  Y := Lowercase(Trim(B));
  while (Length(X) > 0) and (X[Length(X)] = '\') do
    X := Copy(X, 1, Length(X) - 1);
  while (Length(Y) > 0) and (Y[Length(Y)] = '\') do
    Y := Copy(Y, 1, Length(Y) - 1);
  Result := X = Y;
end;

{ True when Part is one of the ';' separated parts of Path (case-insensitive). }
function PathHasPart(const Path, Part: String): Boolean;
var
  Rest: String;
  P: Integer;
begin
  Result := False;
  Rest := Path;
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Result := Result or PartsEqual(Rest, Part);
      Rest := '';
    end
    else
    begin
      Result := Result or PartsEqual(Copy(Rest, 1, P - 1), Part);
      Rest := Copy(Rest, P + 1, Length(Rest));
    end;
  end;
end;

{ Path without Part; every other part stays exactly as it was (section 4.4 removal). }
function PathRemovePart(const Path, Part: String): String;
var
  Rest, One: String;
  P: Integer;
  First: Boolean;
begin
  Result := '';
  First := True;
  Rest := Path;
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      One := Rest;
      Rest := '';
    end
    else
    begin
      One := Copy(Rest, 1, P - 1);
      Rest := Copy(Rest, P + 1, Length(Rest));
    end;
    if (One <> '') and (not PartsEqual(One, Part)) then
    begin
      if not First then
        Result := Result + ';';
      Result := Result + One;
      First := False;
    end;
  end;
end;

{ Numeric TCP port 1024..65535 }
function PortOk(const Text: String; var Port: Integer): Boolean;
begin
  Port := StrToIntDef(Trim(Text), 0);
  Result := (Port >= 1024) and (Port <= 65535);
end;

{ ---- Version compare (design 19.2 item 6: no downgrade) ---- }

{ Part number Index (1-based) of a dotted version: its leading digits as a number, so "14.10.2" part 2
  is 10 and "14.2.2-rc1" part 3 is 2. A missing or non-numeric part is 0, and a leading "v" is ignored.
  Numeric on purpose: as text "14.10" would sort before "14.9". }
function VersionPart(const V: String; const Index: Integer): Integer;
var
  Rest, Digits: String;
  P, I, Len: Integer;
begin
  Rest := Trim(V);
  if (Length(Rest) > 0) and ((Rest[1] = 'v') or (Rest[1] = 'V')) then
    Rest := Copy(Rest, 2, Length(Rest));
  for I := 2 to Index do
  begin
    P := Pos('.', Rest);
    if P = 0 then
    begin
      Result := 0;
      Exit;
    end;
    Rest := Copy(Rest, P + 1, Length(Rest));
  end;
  P := Pos('.', Rest);
  if P > 0 then
    Rest := Copy(Rest, 1, P - 1);
  Digits := '';
  Len := Length(Rest);
  I := 1;
  while (I <= Len) and (Rest[I] >= '0') and (Rest[I] <= '9') do
  begin
    Digits := Digits + Rest[I];
    I := I + 1;
  end;
  Result := StrToIntDef(Digits, 0);
end;

{ -1 when A is older than B, 0 when they are the same version, 1 when A is newer. Up to 8 parts. }
function CompareVersions(const A, B: String): Integer;
var
  I, X, Y: Integer;
begin
  Result := 0;
  for I := 1 to 8 do
  begin
    X := VersionPart(A, I);
    Y := VersionPart(B, I);
    if X < Y then
    begin
      Result := -1;
      Exit;
    end;
    if X > Y then
    begin
      Result := 1;
      Exit;
    end;
  end;
end;

{ True when the installed Cognita (linux_version from `state`) is NEWER than this Setup: running this
  Setup would be a downgrade. An unknown version on either side is never "older" (fresh installs and an
  interrupted first install have no linux_version). }
function SetupIsOlder(const InstalledVersion, SetupVersion: String): Boolean;
begin
  Result := (Trim(InstalledVersion) <> '') and (Trim(SetupVersion) <> '') and
    (CompareVersions(InstalledVersion, SetupVersion) > 0);
end;

{ The refusal text of design 19.2 item 6. }
function DowngradeText(const InstalledVersion, SetupVersion: String): String;
begin
  Result := 'Cognita ' + InstalledVersion + ' is installed; this Setup is older (' + SetupVersion + '). ' +
    'Use a newer Setup, or "cognita rollback" to go back a release.';
end;

{ ---- Wording that depends on the mode (design 19.4 items 10 and 23) ---- }

{ The Finished page heading after a failed run. }
function FailHeading(const Mode: Integer): String;
begin
  if Mode = ModeUpdate then
    Result := CustomMessage('failedUpdateHeading')
  else if Mode = ModeRepair then
    Result := CustomMessage('failedRepairHeading')
  else if Mode = ModeReinstall then
    Result := CustomMessage('failedReinstallHeading')
  else
    Result := CustomMessage('failedInstallHeading');
end;

{ Update, repair and reinstall run over a Cognita that was there before: a failure leaves it as it was
  (design 19.4 item 10). Fresh and finish have no earlier Cognita to speak of. }
function StillThereText(const Mode: Integer): String;
begin
  if (Mode = ModeUpdate) or (Mode = ModeRepair) or (Mode = ModeReinstall) then
    Result := CustomMessage('failedExistingInstallRemains')
  else
    Result := '';
end;

{ The progress page's caption for the install step (design 19.4 item 23). Finish continues an install. }
function ProgressCaption(const Mode: Integer): String;
begin
  if Mode = ModeUpdate then
    Result := CustomMessage('progressUpdating')
  else if Mode = ModeRepair then
    Result := CustomMessage('progressRepairing')
  else if Mode = ModeReinstall then
    Result := CustomMessage('progressReinstalling')
  else
    Result := CustomMessage('progressInstalling');
end;

{ Design 21.4: the Skip self-tests button is shown only while the Linux side is on its `proof` stage and
  the user has not pressed it yet. (The Copy button lives on stage remote.login, so the two are never
  visible together.) }
function ProofSkipVisible(const Stage: String; const Pressed: Boolean): Boolean;
begin
  Result := (Stage = 'proof') and (not Pressed);
end;

{ Design 21.4: the one line the Finished page adds, after the addresses, when the install result carried
  proof=skipped. }
function SkippedProofNote: String;
begin
  Result := 'The self-tests were skipped. To run them later, run this Setup again.';
end;

{ ---- The Ready page's check-failure text (design 19.4 item 18) ---- }

{ Replaces every occurrence of Find in Text, ignoring case. When the match starts with a capital letter
  the replacement's first letter is capitalized too, so a sentence stays a sentence. }
function ReplaceNoCase(const Text, Find, Repl: String): String;
var
  P: Integer;
  First, R: String;
begin
  P := Pos(Lowercase(Find), Lowercase(Text));
  if (Find = '') or (P = 0) then
  begin
    Result := Text;
    Exit;
  end;
  R := Repl;
  First := Copy(Text, P, 1);
  if (R <> '') and (Uppercase(First) = First) and (Lowercase(First) <> First) then
    R := Uppercase(Copy(R, 1, 1)) + Copy(R, 2, Length(R));
  Result := Copy(Text, 1, P - 1) + R +
    ReplaceNoCase(Copy(Text, P + Length(Find), Length(Text)), Find, Repl);
end;

{ The helper's check fixes end "choose other ports under Advanced" and "choose another data location
  under Advanced". Advanced cannot change a value the mode locks, so the text says what to do instead:
  ports in an update: run Setup again after the update; the data location in any mode that fixes it:
  free space on that drive. Anything else in the text is left exactly as it was. }
function RewriteAdvancedHints(const Text: String; const Mode: Integer; const DataLocked: Boolean;
  const Drive: String): String;
var
  Where: String;
begin
  Result := Text;
  if Mode = ModeUpdate then
    Result := ReplaceNoCase(Result, 'choose other ports under Advanced',
      'run Setup again after the update to change ports');
  if DataLocked then
  begin
    Where := Drive;
    if Where = '' then
      Where := 'that drive';
    if Pos(', or choose another data location under advanced', Lowercase(Result)) > 0 then
      Result := ReplaceNoCase(Result, ', or choose another data location under Advanced', ' on ' + Where)
    else
      Result := ReplaceNoCase(Result, 'choose another data location under Advanced', 'free space on ' + Where);
  end;
end;

{ ---- Data location checks (design 19.7 item 16) ---- }

{ "D:" or "D:\" (also "D:/"): a drive root, which the data location may not be. }
function IsDriveRoot(const Path: String): Boolean;
var
  P: String;
begin
  P := Trim(Path);
  while (Length(P) > 0) and ((P[Length(P)] = '\') or (P[Length(P)] = '/')) do
    P := Copy(P, 1, Length(P) - 1);
  Result := (Length(P) = 2) and (P[2] = ':');
end;

{ True when Path is Root or somewhere inside it (case-insensitive, trailing backslashes ignored). An
  empty Root never matches. }
function PathIsUnder(const Path, Root: String): Boolean;
var
  P, R: String;
begin
  P := Lowercase(Trim(Path));
  R := Lowercase(Trim(Root));
  while (Length(P) > 0) and (P[Length(P)] = '\') do
    P := Copy(P, 1, Length(P) - 1);
  while (Length(R) > 0) and (R[Length(R)] = '\') do
    R := Copy(R, 1, Length(R) - 1);
  Result := (R <> '') and ((P = R) or (Copy(P, 1, Length(R) + 1) = R + '\'));
end;

{ A OneDrive-synced folder: inside the folder OneDrive says it syncs (its environment variables,
  passed in) or inside any folder named OneDrive or "OneDrive - <organization>", which is what
  OneDrive names its folders. }
function IsOneDrivePath(const Path, Root1, Root2, Root3: String): Boolean;
var
  Rest, Part: String;
  P: Integer;
begin
  Result := PathIsUnder(Path, Root1) or PathIsUnder(Path, Root2) or PathIsUnder(Path, Root3);
  Rest := Lowercase(Trim(Path));
  while (not Result) and (Rest <> '') do
  begin
    P := Pos('\', Rest);
    if P = 0 then
    begin
      Part := Rest;
      Rest := '';
    end
    else
    begin
      Part := Copy(Rest, 1, P - 1);
      Rest := Copy(Rest, P + 1, Length(Rest));
    end;
    if (Part = 'onedrive') or (Copy(Part, 1, 11) = 'onedrive - ') then
      Result := True;
  end;
end;

{ ---- NVIDIA acceleration and the WSL memory check box (design 22.7, 22.9 and 22.12) ---- }

const
  AccelHidden = 0;                { no Acceleration page: update mode, or no NVIDIA card seen (nvidia is not ok/old) }
  AccelOffer = 1;                 { a card with driver 580 or newer AND this Setup has an NVIDIA build: both choices }
  AccelOldDriver = 2;             { a card whose driver is older than 580: GPU choice disabled, the driver link shown }
  AccelNoBuild = 3;               { a usable card but this Setup has no NVIDIA build (SizeCognitaNvidia = 0): GPU disabled }

{ Which flavor of the Acceleration page this run gets (design 22.7). Update never shows it (the Linux side keeps
  the profile in its env file, design 22.1). `old` wins over a missing build: the driver text is the more useful
  one. Anything but ok/old (none, empty, a word a newer helper might add) hides the page. }
function AccelPageMode(const NvState: String; const NvidiaBuildBytes: Int64; const Mode: Integer): Integer;
var
  S: String;
begin
  S := Lowercase(Trim(NvState));
  if Mode = ModeUpdate then
    Result := AccelHidden
  else if S = 'old' then
    Result := AccelOldDriver
  else if S = 'ok' then
  begin
    if NvidiaBuildBytes > 0 then
      Result := AccelOffer
    else
      Result := AccelNoBuild;
  end
  else
    Result := AccelHidden;
end;

{ cpu, amd or nvidia as the helper reports them (lower case); anything else is "not known" and comes back ''. }
function AccelKnown(const Profile: String): String;
var
  S: String;
begin
  S := Lowercase(Trim(Profile));
  if (S = 'cpu') or (S = 'amd') or (S = 'nvidia') then
    Result := S
  else
    Result := '';
end;

{ The radio the page opens with (design 22.1 and 22.7): NVIDIA only when it is offered AND (a fresh or finish
  install, or the install already runs on NVIDIA). Repair and reinstall keep what the install uses now; an
  unknown or AMD profile counts as CPU for the radio (design 22.12 item 1: AccelArg then does not turn that
  into a switch unless the user changes the radio). }
function AccelDefault(const PageMode, Mode: Integer; const StAccel: String): String;
begin
  if (PageMode = AccelOffer) and ((Mode = ModeFresh) or (Mode = ModeFinish) or (Lowercase(Trim(StAccel)) = 'nvidia')) then
    Result := 'nvidia'
  else
    Result := 'cpu';
end;

{ The pick as the install will act on it: nvidia only when the page offered it, otherwise cpu. }
function AccelPicked(const PageMode: Integer; const Chosen: String): String;
begin
  if (PageMode = AccelOffer) and (Lowercase(Trim(Chosen)) = 'nvidia') then
    Result := 'nvidia'
  else
    Result := 'cpu';
end;

{ The value Setup passes to the helper's --acceleration, or '' when it passes none (design 22.7 as amended
  by 22.12 item 1(b)):
    update                      never (the install keeps its profile);
    fresh / finish              always: the picked profile, or cpu when the page was hidden (the user never
                                chose a GPU, so the Linux side must not pick one because it qualifies);
    repair / reinstall          page hidden: none (keep what is installed); page shown: the picked profile,
                                but only when the user changed the radio (Touched) or the install is known to
                                run on cpu or nvidia, the two profiles the radio can express. An unknown
                                profile (an install from before 15.1) or an AMD one is never turned into a
                                switch by a default the user did not look at. }
function AccelPassed(const PageMode, Mode: Integer; const Chosen: String; const Touched: Boolean;
  const StAccel: String): String;
var
  St: String;
begin
  Result := '';
  St := AccelKnown(StAccel);
  if Mode = ModeUpdate then
    Exit;
  if (Mode = ModeFresh) or (Mode = ModeFinish) then
  begin
    Result := AccelPicked(PageMode, Chosen);
    Exit;
  end;
  if PageMode = AccelHidden then
    Exit;
  if Touched or (St = 'cpu') or (St = 'nvidia') then
    Result := AccelPicked(PageMode, Chosen);
end;

{ The helper option text for the install and update verbs: ' --acceleration <profile>' or ''. }
function AccelArg(const PageMode, Mode: Integer; const Chosen: String; const Touched: Boolean;
  const StAccel: String): String;
var
  P: String;
begin
  P := AccelPassed(PageMode, Mode, Chosen, Touched, StAccel);
  if P = '' then
    Result := ''
  else
    Result := ' --acceleration ' + P;
end;

{ The profile the finished install will have, as far as Setup knows now: what it passes, else what the install
  has (design 22.12 items 1(c) and 10); '' when that is unknown. Drives the Ready line and the size figures. }
function AccelEffective(const PageMode, Mode: Integer; const Chosen: String; const Touched: Boolean;
  const StAccel: String): String;
begin
  Result := AccelPassed(PageMode, Mode, Chosen, Touched, StAccel);
  if Result = '' then
    Result := AccelKnown(StAccel);
end;

{ NVIDIA GPU, AMD GPU or CPU (design 22.7). Anything not nvidia/amd reads as CPU. }
function AccelLabel(const Profile: String): String;
var
  S: String;
begin
  S := Lowercase(Trim(Profile));
  if S = 'nvidia' then
    Result := 'NVIDIA GPU'
  else if S = 'amd' then
    Result := 'AMD GPU'
  else
    Result := 'CPU';
end;

{ The Ready page's value after "Acceleration:  ": the label of the effective profile, or "unchanged" when the
  install keeps a profile Setup does not know (design 22.12 items 1(b) and 1(c)). }
function AccelReadyValue(const Effective: String): String;
begin
  if AccelKnown(Effective) = '' then
    Result := 'unchanged'
  else
    Result := AccelLabel(Effective);
end;

{ The parenthesis after the Ready line when the GPU was not offered (design 22.1 and 22.12 item 10); '' when
  the GPU was offered or in update mode. A hidden page names "no card" only when the helper said none. }
function AccelReadyReason(const PageMode, Mode: Integer; const NvState, Effective: String): String;
begin
  Result := '';
  if Mode = ModeUpdate then
    Exit;
  { 15.1.0 review: a reason only explains a CPU line. A repair with the page hidden keeps the installed profile,
    and "NVIDIA GPU (no NVIDIA graphics card found)" or "unchanged (...)" would contradict itself. }
  if AccelKnown(Effective) <> 'cpu' then
    Exit;
  if PageMode = AccelOldDriver then
    Result := '(NVIDIA driver too old)'
  else if PageMode = AccelNoBuild then
    Result := '(no NVIDIA build in this version)'
  else if (PageMode = AccelHidden) and (Lowercase(Trim(NvState)) = 'none') then
    Result := '(no NVIDIA graphics card found)';
end;

{ The size of the Cognita image the run will download (design 22.7 and 22.12 item 1(c)): the NVIDIA image for an
  nvidia profile that has a build; the larger of the two when the profile is unknown, so the disk check never
  under-sizes; the CPU image otherwise (an AMD install is sized like the CPU one, as before 15.1). }
function AccelCognitaBytes(const Effective, NvState: String; const CpuBytes, NvidiaBytes: Int64): Int64;
var
  E: String;
begin
  E := AccelKnown(Effective);
  Result := CpuBytes;
  if (E = 'nvidia') and (NvidiaBytes > 0) then
    Result := NvidiaBytes
  { 15.1.0 review: an unknown profile on a PC where Windows sees no NVIDIA card cannot be an NVIDIA install (every
    install before 15.1 had no toolkit, and there is no card), so it is sized as CPU; sizing it for NVIDIA made
    every first update from 15.0.x ask for about 4 GB of disk it would never use. }
  else if (E = '') and (Lowercase(Trim(NvState)) <> 'none') and (NvidiaBytes > CpuBytes) then
    Result := NvidiaBytes;
end;

{ The Finished page's line under Workspace (design 22.1, 22.7, 22.12 item 11): only from the result; '' (no
  line) when the result did not say. }
function AccelFinishedLine(const ResultAccel: String): String;
begin
  if AccelKnown(ResultAccel) = '' then
    Result := ''
  else
    Result := FmtMessage(CustomMessage('finishedAcceleration'), [AccelLabel(ResultAccel)]);
end;

{ The fix line of a warning (design 22.7 and 22.12 item 2): the helper's own when it sent one; otherwise, for a
  warning of the `acceleration` stage (the Linux side dropped the GPU), how to try again. }
function WarningFix(const Stage, Fix: String): String;
begin
  if Fix <> '' then
    Result := Fix
  else if Stage = 'acceleration' then
    Result := CustomMessage('warningAccelerationFix')
  else
    Result := '';
end;

{ Keep the helper's English recovery text visible when Setup also shows a localized presentation string. }
function AppendTechnicalDetail(const DisplayFix, TechnicalFix, DetailLabel: String): String;
begin
  if (TechnicalFix = '') or (TechnicalFix = DisplayFix) then
    Result := ''
  else
    Result := #13#10 + DetailLabel + ' ' + TechnicalFix;
end;

{ Design 22.9: the Ready page's WSL memory check box is shown only when wsl_reclaim=unset (no autoMemoryReclaim
  key; `set` and `unreadable` hide it), in every mode, update included. }
function WslReclaimBoxVisible(const WslReclaim: String): Boolean;
begin
  Result := Lowercase(Trim(WslReclaim)) = 'unset';
end;

{ ' --wsl-memory-reclaim' when the box is visible and ticked, else ''. }
function WslReclaimArg(const WslReclaim: String; const Ticked: Boolean): String;
begin
  if WslReclaimBoxVisible(WslReclaim) and Ticked then
    Result := ' --wsl-memory-reclaim'
  else
    Result := '';
end;

{ ==== PURE-END ==== }
