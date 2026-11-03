#ifndef Version
  #error "Pass /DVersion from cargo xtask installer"
#endif
#ifndef PayloadDir
  #error "Pass /DPayloadDir from cargo xtask installer"
#endif
[Setup]
AppId={{66D9F158-5DF1-4B18-86C8-D10FE58B312E}
AppName=Guac Bridge
AppVersion={#Version}
AppVerName=Guac Bridge {#Version}
VersionInfoVersion={#Version}
VersionInfoProductVersion={#Version}
VersionInfoDescription=Guac Bridge {#Version} Setup
SetupIconFile=..\crates\guac-bridge\assets\frog.ico
WizardStyle=modern
AppPublisher=Guac Bridge contributors
AppPublisherURL=https://github.com/himbeles/guac-bridge
DefaultDirName={localappdata}\Programs\Guac Bridge
DefaultGroupName=Guac Bridge
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no
OutputDir=..\dist
OutputBaseFilename=guac-bridge-{#Version}-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\guac-bridge.exe
LicenseFile=..\LICENSE
InfoBeforeFile=runtime-info.txt

[Tasks]
Name: "addtopath"; Description: "Add Guac Bridge to my user PATH"; Flags: checkedonce

[Files]
Source: "..\\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\guac-ssh-client.exe"
Type: files; Name: "{app}\guac-llm-client.exe"

[Icons]
Name: "{group}\Guac Bridge Manager"; Filename: "{app}\guac-bridge.exe"; Parameters: "tray"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var ExitCode: Integer;
begin
  Result := '';
  ExtractTemporaryFile('guac-bridge.exe');
  if not Exec(ExpandConstant('{tmp}\guac-bridge.exe'), 'prepare-update', '', SW_HIDE,
              ewWaitUntilTerminated, ExitCode) then
    Result := 'Could not stop Guac Bridge before updating. Close its bridges and tray, then retry.'
  else if ExitCode <> 0 then
    Result := 'Guac Bridge shutdown failed. A bridge may still be starting, or the tray may have a dialog open. Close it and retry; no files have been replaced.';
end;

function HasPath(Value, Item: String): Boolean;
begin
  Result := Pos(';' + Lowercase(Item) + ';', ';' + Lowercase(Value) + ';') > 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Current, InstallPath: String;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then begin
    InstallPath := ExpandConstant('{app}');
    RegQueryStringValue(HKCU, 'Environment', 'Path', Current);
    if not HasPath(Current, InstallPath) then begin
      if (Current <> '') and (Current[Length(Current)] <> ';') then Current := Current + ';';
      RegWriteStringValue(HKCU, 'Environment', 'Path', Current + InstallPath);
      RegWriteDWordValue(HKCU, 'Software\Guac Bridge', 'AddedToPath', 1);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Current, Updated, Item: String; Added: Cardinal; I, Start: Integer;
begin
  if CurUninstallStep = usPostUninstall then begin
    if RegQueryDWordValue(HKCU, 'Software\Guac Bridge', 'AddedToPath', Added) and (Added = 1) then begin
      RegQueryStringValue(HKCU, 'Environment', 'Path', Current);
      Updated := ''; Start := 1;
      for I := 1 to Length(Current) + 1 do begin
        if (I > Length(Current)) or (Current[I] = ';') then begin
          Item := Copy(Current, Start, I - Start);
          if Lowercase(Item) <> Lowercase(ExpandConstant('{app}')) then begin
            if Updated <> '' then Updated := Updated + ';';
            Updated := Updated + Item;
          end;
          Start := I + 1;
        end;
      end;
      RegWriteStringValue(HKCU, 'Environment', 'Path', Updated);
      RegDeleteValue(HKCU, 'Software\Guac Bridge', 'AddedToPath');
    end;
  end;
end;
