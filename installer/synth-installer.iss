; SyntH Windows installer — Inno Setup 6 script
;
; Build with:  iscc installer\synth-installer.iss
; Or run:      installer\build_installer.ps1
;
; Design rules for this installer (see one-click.md):
;   * One option, no choices. No component picker, no types, no directory page.
;   * No admin. Everything lands in %LOCALAPPDATA% and the user profile.
;   * No console windows. Every helper runs hidden and is launched through
;     pythonw.exe so nothing flashes on screen.
;   * Personal details (name, location, timezone, engines, API keys) are NOT
;     asked here. They belong to the WebUI setup page, which the finish page
;     opens for you.

#define AppName "Synthetic Heart"
#define AppShortName "SyntH"
#define AppPublisher "Synthetic Heart Project"
#define AppURL "https://github.com/XargonWan/Synthetic_Heart"

; The version is supplied by the caller:  iscc /DAppVersion=1.2.3 installer\synth-installer.iss
; installer\build_installer.ps1 derives it (git tag, GitVersion, or a fallback)
; and CI passes the GitVersion output, which is the single source of truth for
; releases (see GitVersion.yml). There is no version file in the repository.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

; Windows version resources must be numeric, so the caller passes the numeric
; run explicitly: "1.0.0" for both "1.0.0a" (a revision of 1.0.0) and
; "1.2.3-feat.4" (a pre-release). Falling back to the hyphen rule keeps an older
; caller working, and AppVersion keeps the full string everywhere the user sees
; it (the installer's own name, its title bar and Add/Remove Programs).
#ifndef AppVersionNumeric
  #if Pos("-", AppVersion) > 0
    #define AppVersionNumeric Copy(AppVersion, 1, Pos("-", AppVersion) - 1)
  #else
    #define AppVersionNumeric AppVersion
  #endif
#endif

; The default persona (skins\Rei) always ships, because the avatar has to work
; out of the box. The example personas are about 80 MB of models between them,
; which is a lot to download for something most people never switch to, so they
; are opt-in at build time:
;   iscc /DWithExampleSkins=1 installer\synth-installer.iss
#ifndef WithExampleSkins
  #define ExampleSkinsExclude "skins\Zero\*,skins\Miku\*,skins\Riko\*,"
#else
  #define ExampleSkinsExclude ""
#endif

[Setup]
AppId={{8F3A4B2C-9D1E-4F7A-B5C6-2E8D0A3F1B9E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersionNumeric}

; User scope: no UAC prompt, and the app directory is writable by the app.
DefaultDirName={localappdata}\Programs\{#AppShortName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; No questions: no directory page, no program group page, no component page.
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
AllowNoIcons=yes

DefaultGroupName={#AppShortName}
OutputDir=Output
OutputBaseFilename=SyntH-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile=synth.ico
WizardImageFile=wizard-large.bmp
WizardSmallImageFile=wizard-small.bmp
UninstallDisplayIcon={app}\installer\synth.ico
UninstallDisplayName={#AppName}
MinVersion=10.0.16299

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Welcome to the {#AppName} setup
WelcomeLabel2=This installs {#AppName} for you and nothing else.%n%nIt sets up its own private PostgreSQL and Python environment inside your user folder, so it needs no administrator rights and changes nothing system-wide.%n%nWhen it finishes it opens a page in your browser where you tell it who you are and which AI service to use. Everything else is already done.%n%nClick Next to continue.
FinishedHeadingLabel=Setup is complete
FinishedLabel=Setup has finished installing {#AppName}.%n%nIt is starting now and your browser will open the setup page. If it does not, use the {#AppShortName} shortcut in the Start Menu.
ReadyLabel1={#AppName} is ready to install.
ReadyLabel2a=Click Install to begin.
SelectDirLabel3=Setup will install {#AppName} into the following folder.

; The confirmation Inno shows after the uninstaller's own window. The stock text says
; "all of its components" and "successfully removed", which is not what happens here: the
; data is kept unless the user ticked the box. Both are reworded to say so. Note Inno's
; dialog defaults to No, so a click-through leaves the install alone.
ConfirmUninstall={#AppName} will be removed now.%n%nYour data is kept unless you ticked the box on the previous window.%n%nClick Yes to continue, or No to leave it installed.
UninstalledAll={#AppName} was removed.%n%nIf you kept your data, installing again resumes from where you left off.
UninstalledMost={#AppName} was removed, except for a few files that could not be deleted.%n%nIf you kept your data, installing again resumes from where you left off.

[Tasks]
; Unchecked by default: the Start Menu entry is enough for most people.
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
; Nothing is asked here about what an uninstall should do with the data. That question
; belongs in the uninstaller, where it is a decision about something that is actually
; happening; asked during setup it is a question about a hypothetical future, and the
; answer is stored for months before it is used. See InitializeUninstall in [Code].

[Files]
; The application tree. Generated and personal things are deliberately excluded:
; data\ holds the database, attachments and your encrypted API keys, and .env
; holds your settings. Shipping either would leak one install into another.
; NOTE: the Excludes list must stay on ONE line. A trailing backslash inside a
; quoted string is not a line continuation to Inno, it is a literal character,
; and the compiler then fails with "The system cannot find the path specified".
Source: "..\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs; Excludes: ".git\*,.github\*,.venv*,.venv\*,logs\*,data\*,__pycache__\*,**\__pycache__\*,*.pyc,*.pyo,*.pyd,.tools\*,.gitnexus\*,.claude\*,.clinerules*,.codex\*,.continue\*,.cursor\*,.gemini\*,.zed\*,.idea\*,.vscode\*,node_modules\*,frontend\node_modules\*,mcp_servers\*,*.egg-info\*,docs\res\*,docs\wiki\*,installer\Output\*,installer\vendor\*,installer\*.iss,installer\*.ps1,installer\wizard-*.bmp,installer\synth-256.png,{#ExampleSkinsExclude}skins\2B\*,skins\temp\*,backups\*,tmp*,dist\*,build\*,site\*,htmlcov\*,.mypy_cache\*,.pytest_cache\*,.tox\*,.ruff_cache\*,old\*,SyntH_main,REWRITE-TASK.mm,SOUL-REWRITE-TASK.md,*.bak,*.swp,*.orig,*.log,*.log.*,.env,.env-*,.env.local,VENICE_NO_RESPONSE_REPORT.md,pr.md,one-click.md,res\synth_webui\static\audio\tts\*"

; pgvector for the PostgreSQL we provision. Built by CI, absent in a source
; checkout, hence skipifsourcedoesntexist: the app degrades to in-memory SOUL
; memory instead of refusing to install.
Source: "vendor\pgvector\*"; DestDir: "{app}\installer\vendor\pgvector"; \
  Flags: recursesubdirs ignoreversion skipifsourcedoesntexist

[Icons]
; pythonw.exe, not a .bat: no console window ever appears.
Name: "{group}\{#AppShortName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\start_synth.py"""; WorkingDir: "{app}"; \
  IconFilename: "{app}\installer\synth.ico"; Comment: "Start {#AppName}"
Name: "{group}\{#AppShortName} setup page"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\start_synth.py"" --setup"; WorkingDir: "{app}"; \
  IconFilename: "{app}\installer\synth.ico"; Comment: "Open the setup page"
Name: "{group}\Uninstall {#AppShortName}"; Filename: "{uninstallexe}"; \
  Parameters: "/SILENT /SYNTHASK=1"
Name: "{autodesktop}\{#AppShortName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\start_synth.py"""; WorkingDir: "{app}"; \
  IconFilename: "{app}\installer\synth.ico"; Tasks: desktopicon

[Run]
; Runs last, from the finish page, after the database and environment exist.
; --setup matters: without it the launcher opens the plain WebUI and a new user
; meets the avatar scene with nothing telling them what to do next.
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\scripts\start_synth.py"" --setup"; \
  WorkingDir: "{app}"; StatusMsg: "Starting {#AppName}..."; \
  Flags: postinstall nowait skipifsilent; \
  Description: "Start {#AppName} and open the setup page"

[UninstallRun]
; Stop the app first so it releases the database and the log files.
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\scripts\start_synth.py"" --stop"; \
  Flags: runhidden waituntilterminated; RunOnceId: "StopSyntH"
; Then the private PostgreSQL cluster: Windows will not delete files a running
; process holds open, and a stale cluster on a stale port breaks the next install.
Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\bootstrap.py"" --stop-cluster --pg-bin ""{app}\pgsql\bin"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "StopSyntHDatabase"

[UninstallDelete]
; Only what the installer created or the app generated. data\ and .env are not in this
; list at all: whether they survive is decided in the uninstaller, where the user is
; asked, and answered by InitializeUninstall/CurUninstallStepChanged in [Code].
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\pgsql"
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: filesandordirs; Name: "{app}\.pytest_cache"
Type: filesandordirs; Name: "{app}\.ruff_cache"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\uv.lock"

[Code]
const
  PrereqsLog = 'synth_prereqs.log';
  BootstrapLog = 'synth_bootstrap.log';

function StepFailed(const Title, Detail, LogName: String): Boolean;
var
  LogPath: String;
begin
  { The temp folder below is the same one install_prereqs.ps1 (its env:TEMP)
    and bootstrap.py write their logs into, so the path named in this message is
    a real one. The user profile constant is the percent-USERPROFILE form: a
    bare lowercase name such as the obvious-looking "userprofile" is not a
    constant at all, and because ExpandConstant is only evaluated here it fails
    at runtime rather than at compile time. tests/test_installer_payload.py
    guards against exactly that. No braces inside this comment: Inno comments
    do not nest, so an inner one would end the comment early. }
  LogPath := ExpandConstant('{%TEMP}\') + LogName;
  MsgBox(Title + #13#10#13#10 + Detail + #13#10#13#10 +
    'The log is here:' + #13#10 + LogPath + #13#10#13#10 +
    'You can retry this step yourself from a terminal in:' + #13#10 + ExpandConstant('{app}'),
    mbError, MB_OK);
  Result := False;
end;

function RunHidden(const Exe, Params: String; var ResultCode: Integer): Boolean;
begin
  Result := Exec(Exe, Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  UvExe: String;
  Powershell: String;
begin
  if CurStep = ssDone then
  begin
    { Add/Remove Programs runs whatever is in UninstallString, and the uninstaller skips
      its own confirmation whenever it is launched silently. Pointing the entry at
      /SILENT is therefore what leaves this script's window as the only one the user
      sees: without it, Inno asks "are you sure" *after* that window, so the same
      question arrives twice. /SYNTHASK=1 marks this as the ask-anyway path, because a
      silent run is normally a scripted one that must not block on a dialog.
      Rewritten at ssDone, when the uninstaller and its key certainly exist. }
    { The uninstall key is Inno's own: AppId with its escaped braces written out, because
      a script cannot read a [Setup] directive back as raw text. tests/test_installer_
      payload.py asserts this GUID and the AppId directive carry the same GUID. }
    RegWriteStringValue(HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3A4B2C-9D1E-4F7A-B5C6-2E8D0A3F1B9E}_is1',
      'UninstallString',
      '"' + ExpandConstant('{app}') + '\unins000.exe" /SILENT /SYNTHASK=1');
    exit;
  end;

  if CurStep <> ssPostInstall then
    exit;

  Powershell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');

  { Step 1: uv, PostgreSQL and pgvector. }
  WizardForm.StatusLabel.Caption := 'Installing dependencies (uv, PostgreSQL)...';
  if not RunHidden(Powershell,
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\scripts\install_prereqs.ps1') +
      '" -InstallDir "' + ExpandConstant('{app}') + '" -Quiet',
      ResultCode) or (ResultCode <> 0) then
  begin
    StepFailed('Dependencies could not be installed.',
      'install_prereqs.ps1 exited with code ' + IntToStr(ResultCode) + '.', PrereqsLog);
    exit;
  end;

  { Step 2: database, .env, Python environment. }
  WizardForm.StatusLabel.Caption := 'Setting up the database and Python environment...';
  UvExe := ExpandConstant('{%USERPROFILE}\.local\bin\uv.exe');
  if not FileExists(UvExe) then
    UvExe := 'uv';

  if not RunHidden(UvExe,
      'run --no-project python "' + ExpandConstant('{app}\scripts\bootstrap.py') +
      '" --portable --pg-bin "' + ExpandConstant('{app}\pgsql\bin') +
      '" --no-browser --log-file "' + ExpandConstant('{%TEMP}\' + BootstrapLog) + '"',
      ResultCode) or (ResultCode <> 0) then
  begin
    StepFailed('The database or the Python environment could not be set up.',
      'bootstrap.py exited with code ' + IntToStr(ResultCode) + '.', BootstrapLog);
    exit;
  end;
end;

var
  DeleteDataOnUninstall: Boolean;

function InitializeUninstall(): Boolean;
var
  Form: TSetupForm;
  Heading, Body: TNewStaticText;
  DataCheck: TNewCheckBox;
  RemoveButton, CancelButton: TNewButton;
begin
  { Asked here, at uninstall, where it is a decision about something that is actually
    happening. Setup used to carry it as an unchecked task, which asked the user to
    imagine a future uninstall while they were still installing and then stored the
    answer for months.

    Keeping the data is the default and the only silent answer: a silent uninstall has
    nobody to ask, and keeping is the direction that can be undone afterwards by
    deleting the paths again, while deleting is not.

    This replaced a message box whose default button was Yes. Clicking through a dialog
    is how most people leave one, so the click-through deleted the persona, the chat
    history and the database. A checkbox that starts clear cannot do that: the window
    says what stays and what goes before anything is removed, and the default answer is
    the recoverable one. One window, like the installer.

    No braces in these comments: Inno comments do not nest, and an inner one would end
    the comment early. }
  DeleteDataOnUninstall := False;
  Result := True;
  { Shown only when the caller asks for it with /SYNTHASK=1. Add/Remove Programs and the
    Start-menu entry both pass that alongside /SILENT, which is what makes Inno skip its
    own confirmation and leave this as the single window. Without the flag - a scripted
    run, or unins000.exe by hand - this script never shows a dialog, and the data is
    kept, because an unattended uninstall must not block and must not destroy. }
  if ExpandConstant('{param:SYNTHASK|0}') = '1' then
  begin
    Form := CreateCustomForm(ScaleX(480), ScaleY(300), False, True);
    try
      Form.Caption := ExpandConstant('{#AppName} uninstall');
      Form.BorderStyle := bsDialog;
      Form.ClientWidth := ScaleX(480);
      Form.ClientHeight := ScaleY(300);
      Form.Position := poScreenCenter;

      Heading := TNewStaticText.Create(Form);
      Heading.Parent := Form;
      Heading.Left := ScaleX(20);
      Heading.Top := ScaleY(20);
      Heading.Width := Form.ClientWidth - ScaleX(40);
      Heading.AutoSize := False;
      Heading.Font.Style := [fsBold];
      Heading.Font.Size := Form.Font.Size + 3;
      Heading.Caption := 'Remove {#AppName}?';

      Body := TNewStaticText.Create(Form);
      Body.Parent := Form;
      Body.Left := ScaleX(20);
      Body.Top := ScaleY(56);
      Body.Width := Form.ClientWidth - ScaleX(40);
      Body.Height := ScaleY(104);
      Body.AutoSize := False;
      Body.WordWrap := True;
      Body.Caption := 'The application, its private database engine and the Python '
        + 'environment it runs in are removed.'
        + #13#10 + #13#10
        + 'Your own things stay, unless you tick the box below: the persona, the chat '
        + 'history, the memories, your uploaded files, the database in data and the '
        + 'credentials and API keys in .env. Installing again later picks up where this '
        + 'one left off.';

      DataCheck := TNewCheckBox.Create(Form);
      DataCheck.Parent := Form;
      DataCheck.Left := ScaleX(20);
      DataCheck.Top := ScaleY(170);
      DataCheck.Width := Form.ClientWidth - ScaleX(40);
      DataCheck.Height := ScaleY(42);
      DataCheck.Caption := 'Also delete all of my data and settings'
        + #13#10
        + 'The persona, chats, memories, uploads, database and .env. Cannot be undone.';
      DataCheck.Checked := False;
      DataCheck.TabOrder := 0;

      RemoveButton := TNewButton.Create(Form);
      RemoveButton.Parent := Form;
      RemoveButton.Width := ScaleX(110);
      RemoveButton.Height := ScaleY(25);
      RemoveButton.Left := Form.ClientWidth - ScaleX(20) - RemoveButton.Width;
      RemoveButton.Top := Form.ClientHeight - ScaleY(20) - RemoveButton.Height;
      RemoveButton.Caption := 'Uninstall';
      RemoveButton.ModalResult := mrOk;
      RemoveButton.Default := True;
      RemoveButton.TabOrder := 2;

      CancelButton := TNewButton.Create(Form);
      CancelButton.Parent := Form;
      CancelButton.Width := RemoveButton.Width;
      CancelButton.Height := RemoveButton.Height;
      CancelButton.Left := RemoveButton.Left - ScaleX(8) - CancelButton.Width;
      CancelButton.Top := RemoveButton.Top;
      CancelButton.Caption := 'Cancel';
      CancelButton.ModalResult := mrCancel;
      CancelButton.Cancel := True;
      CancelButton.TabOrder := 1;

      Form.ActiveControl := RemoveButton;
      { Cancel, or closing the window, leaves everything installed: the uninstall has not
        touched anything at this point. }
      if Form.ShowModal() <> mrOk then
      begin
        Result := False;
        exit;
      end;
      DeleteDataOnUninstall := DataCheck.Checked;
    finally
      Form.Free;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { usPostUninstall, not usUninstall: the two [UninstallRun] entries have stopped the
    application and its PostgreSQL cluster by then, and a cluster that is still running
    holds its files open, which would leave the install half deleted. }
  if (CurUninstallStep = usPostUninstall) and DeleteDataOnUninstall then
  begin
    DelTree(ExpandConstant('{app}\data'), True, True, True);
    DeleteFile(ExpandConstant('{app}\.env'));
    { Then the rest of the folder, because "delete all of my data" cannot leave a folder
      of leftovers behind for the next install to inherit. Inno has finished its own
      removal by this step and the uninstaller runs from a copy in TEMP, so wiping the
      folder it was launched from is safe. }
    DelTree(ExpandConstant('{app}'), True, True, True);
  end;
end;
