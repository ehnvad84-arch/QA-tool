; Установщик Windows. Собирается из build\pyinstaller\qa-tool: iscc /DVersion=0.1.0 packaging\windows\qa-tool.iss
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{6B7E5F2C-3D4A-4E8B-9C1F-5A0B7C0D1E01}
AppName=QA-tool
AppVersion={#Version}
AppPublisher=Рувер
DefaultDirName={autopf}\QA-tool
DefaultGroupName=QA-tool
OutputDir=..\..\dist
OutputBaseFilename=QA-tool-{#Version}-setup
SetupIconFile=..\icon.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "..\..\build\pyinstaller\qa-tool\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\QA-tool"; Filename: "{app}\qa-tool.exe"
Name: "{autodesktop}\QA-tool"; Filename: "{app}\qa-tool.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Значок на рабочем столе"; Flags: unchecked

[Run]
Filename: "{app}\qa-tool.exe"; Description: "Запустить QA-tool"; Flags: nowait postinstall skipifsilent
