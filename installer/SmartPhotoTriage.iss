#define MyAppName "Smart Photo Triage"
#define MyAppVersion "1.2.1"
#define MyAppPublisher "Smart Photo Triage contributors"
#define MyAppExeName "Smart Photo Triage.exe"

[Setup]
AppId={{BE70455D-9FC6-4D4E-B7E4-D1865CEAFB84}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Smart Photo Triage
DefaultGroupName=Smart Photo Triage
UninstallDisplayName=Smart Photo Triage
OutputDir=..\release\installer
OutputBaseFilename=Smart-Photo-Triage-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

[Files]
Source: "..\release\dist\Smart Photo Triage\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Smart Photo Triage"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Smart Photo Triage"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项："; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 Smart Photo Triage"; Flags: nowait postinstall skipifsilent
