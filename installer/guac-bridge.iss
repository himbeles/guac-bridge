#ifndef Version
  #define Version "1.0.1"
#endif
[Setup]
AppId={{66D9F158-5DF1-4B18-86C8-D10FE58B312E}
AppName=Guac Bridge
AppVersion={#Version}
AppPublisher=Guac Bridge contributors
AppPublisherURL=https://github.com/himbeles/guac-bridge
DefaultDirName={localappdata}\Programs\Guac Bridge
DefaultGroupName=Guac Bridge
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
OutputDir=..\dist
OutputBaseFilename=guac-bridge-{#Version}-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\guac-bridge.exe
LicenseFile=..\LICENSE

[Tasks]
Name: "addtopath"; Description: "Add Guac Bridge to my user PATH"; Flags: checkedonce

[Files]
Source: "..\\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\target\release\guac-bridge.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\target\release\guac-ssh-client.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\target\release\guac-llm-client.exe"; DestDir: "{app}"; Flags: ignoreversion

[Code]
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
