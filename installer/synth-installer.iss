; SyntH Windows Installer — Inno Setup 6 script
; Build with: iscc installer\synth-installer.iss
; Or run:     installer\build_installer.ps1

#define AppName "Synthetic Heart"
#define AppPublisher "Synthetic Heart Project"
#define AppURL "https://github.com/synthetic-heart/synthetic-heart"
; Read version from version.txt one level up
#define AppVersion Trim(FileRead(FileOpen(AddBackslash(SourcePath) + "..\version.txt")))
; MariaDB MSI filename — must match the file in installer/vendor/
#define MariaDbMsi "mariadb-11.4.5-winx64.msi"

[Setup]
AppId={{8F3A4B2C-9D1E-4F7A-B5C6-2E8D0A3F1B9E}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
; Install to %LOCALAPPDATA%\SyntH — user-writable, no UAC for app dir day-to-day
DefaultDirName={localappdata}\SyntH
DisableProgramGroupPage=no
DefaultGroupName=SyntH
; admin required for MariaDB MSI + service registration; app installs to localappdata by design
PrivilegesRequired=admin
UsedUserAreasWarning=no
OutputDir=Output
OutputBaseFilename=SyntH-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Minimum Windows 10 1709
MinVersion=10.0.16299

[Types]
Name: "standard"; Description: "Standard (MariaDB + Python + Node.js)"
Name: "full";     Description: "Full (+ PostgreSQL for SOUL long-term memory)"
Name: "custom";   Description: "Custom"; Flags: iscustom

[Components]
Name: "mariadb";  Description: "MariaDB 11.4  -  Primary database (required, stores chat history / memory / config)"; \
  Types: standard full custom; Flags: fixed
Name: "postgres"; Description: "PostgreSQL 16  -  SOUL long-term memory / pgvector semantic search (optional)"; \
  Types: full custom
Name: "python";   Description: "Python 3.11  -  Runtime (skipped automatically if already installed)"; \
  Types: standard full custom; Flags: fixed
Name: "nodejs";   Description: "Node.js LTS  -  MCP servers and GitNexus code intelligence (skipped if already installed)"; \
  Types: standard full custom

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nSyntH is a modular AI persona system. It requires MariaDB (bundled), Python 3.11+, and the uv package manager.%n%nClick Next to continue.

[Dirs]
Name: "{app}\logs"
Name: "{app}\data"
Name: "{app}\tools"

[Files]
; ── SyntH source tree (entire repo minus generated/binary dirs) ──────────��──
Source: "..\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; \
  Excludes: ".git\*,.venv*,logs\*,__pycache__\*,*.pyc,*.pyo,*.pyd,installer\vendor\*,installer\Output\*,.gitnexus\*,.github\*,.claude\*,.clinerules*,.codex\*,.continue\*,.cursor\*,.gemini\*,.zed\*,skins\2B\*,skins\temp\*,data\*,.env*,backups\*,node_modules\*,mcp_servers\*,tmp*,dist\*,build\*,site\*,htmlcov\*,.mypy_cache\*,.pytest_cache\*,.tox\*,.ruff_cache\*,.vscode\*,old\*,SyntH_main,REWRITE-TASK.mm,SOUL-REWRITE-TASK.md,*.bak,*.swp,*.orig,*.log,*.log.*"

; ── Vendor: MariaDB MSI (exact filename — wildcards don't work with msiexec) --
Source: "vendor\{#MariaDbMsi}"; DestDir: "{tmp}"; Flags: deleteafterinstall

; ── Vendor: NSSM service manager ────────────────────────────────────────────
Source: "vendor\nssm.exe"; DestDir: "{app}\tools"; Flags: ignoreversion

[Run]
; ── Steps 1-4: MariaDB + Postgres + Python + uv + Node.js (logged, pauses) ---
; PowerShell spawned directly — Windows creates an interactive console for it.
; ReadKey() in the script reads from that console, not from stdin.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install_prereqs.ps1"" -MariaDbMsi ""{tmp}\{#MariaDbMsi}"" -InstallPostgres {code:IsPostgresSelected}"; \
  WorkingDir: "{app}"; \
  StatusMsg: "Installing prerequisites (MariaDB, Python, uv, Node.js)..."; \
  Flags: waituntilterminated

; ── Step 5: Setup wizard (post-install, user-visible, can be unchecked) ------
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\setup_launcher.ps1"""; \
  WorkingDir: "{app}"; \
  StatusMsg: "Running SyntH setup wizard..."; \
  Flags: postinstall waituntilterminated; \
  Description: "Run SyntH setup wizard (configure database, API keys, embedder, etc.)"

[Icons]
; Start Menu
Name: "{group}\SyntH";                 Filename: "{app}\scripts\start_synth.bat";   WorkingDir: "{app}"
Name: "{group}\SyntH Setup Wizard";   Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -Command ""& python '{app}\scripts\windows_setup.py'"" "; WorkingDir: "{app}"
Name: "{group}\Install a Module";     Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -Command ""& python '{app}\scripts\module_installer.py' list; pause"" "; WorkingDir: "{app}"
Name: "{group}\Open WebUI";           Filename: "http://localhost:8001"
Name: "{group}\Uninstall SyntH";      Filename: "{uninstallexe}"

; Desktop shortcut
Name: "{commondesktop}\SyntH";        Filename: "{app}\scripts\start_synth.bat";   WorkingDir: "{app}"

[UninstallDelete]
; Remove directories created after install (not tracked by Inno Setup)
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\.gitnexus"
Type: filesandordirs; Name: "{app}\plugins"
Type: filesandordirs; Name: "{app}\core"
Type: filesandordirs; Name: "{app}\engines"
Type: filesandordirs; Name: "{app}\interface"
Type: files;          Name: "{app}\.env"
Type: files;          Name: "{app}\uv.lock"

[UninstallRun]
; Stop + remove SyntH Windows service if registered
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -Command ""if (Get-Service SyntH -ea SilentlyContinue) {{ & '{app}\tools\nssm.exe' remove SyntH confirm }}"" "; \
  Flags: waituntilterminated; RunOnceId: "StopSyntHService"

; Uninstall MariaDB via its own MSI product code (looked up from registry)
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -Command ""$pkg = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' -ea SilentlyContinue | Get-ItemProperty -ea SilentlyContinue | Where-Object {{ $_.DisplayName -match 'MariaDB' }} | Select-Object -First 1; if ($pkg) {{ Write-Host 'Removing MariaDB...'; Start-Process msiexec.exe -ArgumentList ""/x $($pkg.PSChildName) /quiet /norestart"" -Wait; Write-Host 'MariaDB removed.' }} else {{ Write-Host 'MariaDB not found in registry, skipping.' }}"" "; \
  Flags: waituntilterminated; RunOnceId: "UninstallMariaDB"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

function IsPostgresSelected(Param: String): String;
begin
  if WizardIsComponentSelected('postgres') then
    Result := '1'
  else
    Result := '0';
end;
