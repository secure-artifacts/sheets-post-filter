#ifndef MyAppVersion
  #define MyAppVersion "1.1.1"
#endif

#define MyAppName "数据汇总工具"
#define MyAppExeName "数据汇总工具.exe"

[Setup]
AppId={{9D47EB5B-1B34-4AE5-88D3-C3B20D8AC104}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=secure-artifacts
AppPublisherURL=https://github.com/secure-artifacts/sheets-post-filter
AppUpdatesURL=https://github.com/secure-artifacts/sheets-post-filter/releases/latest
DefaultDirName={localappdata}\sheets-post-filter
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=release
OutputBaseFilename=data-summary-tool-setup-v{#MyAppVersion}
SetupIconFile=logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "release\data-summary-tool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动{#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
