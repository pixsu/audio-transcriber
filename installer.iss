; Audio Transcriber - Windows installer (Inno Setup 6, free: https://jrsoftware.org/isinfo.php)
;
; Built by build.bat when Inno Setup is installed:
;   ISCC.exe /DAppVersion=1.6.0 installer.iss
; Output: dist\AudioTranscriber-Setup.exe
;
; Installs for the current user only (no administrator rights needed) into
;   %LOCALAPPDATA%\Programs\Audio Transcriber
; so the program can keep config.json, its log and the downloaded models
; next to the .exe. Installing a newer version over an older one keeps them.

#ifndef AppVersion
  #error Pass the version, e.g. ISCC.exe /DAppVersion=1.6.0 installer.iss
#endif

[Setup]
; Never change AppId: Windows uses it to recognise updates of this program.
AppId={{D33BD043-D799-4510-AE85-AF0745DDC7AA}
AppName=Audio Transcriber
AppVersion={#AppVersion}
AppVerName=Audio Transcriber {#AppVersion}
AppPublisher=pixsu
AppPublisherURL=https://github.com/pixsu/audio-transcriber
AppSupportURL=https://github.com/pixsu/audio-transcriber/issues
DefaultDirName={localappdata}\Programs\Audio Transcriber
DefaultGroupName=Audio Transcriber
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=AudioTranscriber-Setup
SetupIconFile=build\icon.ico
UninstallDisplayIcon={app}\AudioTranscriber.exe
UninstallDisplayName=Audio Transcriber
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The [Code] section asks a running copy to quit cleanly first; Windows'
; "close applications" step is only a fallback.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "&Start Audio Transcriber when I log in (hidden in the tray)"; GroupDescription: "Options:"; Flags: unchecked

[InstallDelete]
; Remove the old program libraries first, so an update never mixes versions.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "build\staging\AudioTranscriber\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Audio Transcriber"; Filename: "{app}\AudioTranscriber.exe"
Name: "{autodesktop}\Audio Transcriber"; Filename: "{app}\AudioTranscriber.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AudioTranscriber"; ValueData: """{app}\AudioTranscriber.exe"" --minimized"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\AudioTranscriber.exe"; Description: "Start Audio Transcriber now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Settings, log and downloaded models live next to the .exe. Your Inbox and
; Transcripts folders (in Documents) are never touched.
Type: filesandordirs; Name: "{app}\models"
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\transcriber.log*"

[Code]
const
  RunningMutex = 'AudioTranscriberAppRunning';

// Ask a running Audio Transcriber to quit cleanly ("--quit" waits until it
// has). A file being transcribed is simply started again next time.
procedure CloseRunningProgram(Exe: String);
var
  ResultCode: Integer;
begin
  if CheckForMutexes(RunningMutex) and FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CheckForMutexes(RunningMutex) then
  begin
    if SuppressibleMsgBox('Audio Transcriber is running and will be closed to install the ' +
        'update. If a file is being transcribed, it starts again when the program reopens.',
        mbInformation, MB_OKCANCEL, IDOK) = IDCANCEL then
    begin
      Result := 'Installation cancelled. Close Audio Transcriber and run Setup again.';
      exit;
    end;
    CloseRunningProgram(ExpandConstant('{app}\AudioTranscriber.exe'));
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  CloseRunningProgram(ExpandConstant('{app}\AudioTranscriber.exe'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // "Start with Windows" may have been switched on from inside the program.
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'AudioTranscriber');
end;
