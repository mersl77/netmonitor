; Inno Setup 6 – instalátor NetMonitoru (bez administrátorských práv, do profilu uživatele)
; Sestavení:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\netmonitor.iss
; (build_windows.ps1 to udělá automaticky, pokud je Inno Setup nainstalovaný)

#define AppName "NetMonitor"
#define AppVersion "1.4.0"
#define AppExe "NetMonitor.exe"

[Setup]
AppId={{6E1C5B7A-3C2E-4C7B-9C0E-6B7E2B8A1D21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Zelina
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=NetMonitor-{#AppVersion}-setup
SetupIconFile=..\..\src\netmonitor\resources\icons\netmonitor.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "czech"; MessagesFile: "compiler:Languages\Czech.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Vytvořit zástupce na ploše"; Flags: unchecked
Name: "autostart"; Description: "Spouštět NetMonitor po přihlášení"

[Files]
Source: "..\..\dist\NetMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Odinstalovat {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; stejná hodnota, jakou nastavuje aplikace v Nastavení → Obecné → Automatické spuštění
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NetMonitor"; \
  ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Spustit NetMonitor"; Flags: nowait postinstall skipifsilent
; automatická aktualizace z aplikace (tichá instalace) – po dokončení aplikaci znovu spustit
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "KillNetMonitor"
