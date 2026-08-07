; Inno Setup script for the TallyBridge on-premise server.
;
; Produces ONE elevated setup.exe containing PostgreSQL, PostgREST, Caddy, WinSW,
; node.exe and the built Express backend. The client machine needs nothing
; pre-installed -- no Docker, no PostgreSQL installer, no Node.
;
; Built by ..\build-installer.ps1, which stages the payload first. Do not run
; ISCC on this directly: the staging step is what trims PostgreSQL from 848 MB to
; 117 MB and builds the backend.

#define AppName        "TallyBridge Server"
#define AppShortName   "TallyBridgeServer"
#define AppPublisher   "TallyBridge"
#define AppVersion     GetEnv('TB_INSTALLER_VERSION')
#define PayloadDir     GetEnv('TB_PAYLOAD_DIR')
#define OutputDir      GetEnv('TB_OUTPUT_DIR')

#if AppVersion == ""
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{8F3A6C21-5D74-4E9B-9C2A-TALLYBRIDGE01}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename={#AppShortName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
; Registering Windows services needs admin, and so does writing to Program Files.
; There is no meaningful per-user install of this, so demand elevation up front
; rather than failing halfway through the post-install script.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName={#AppName}
; PostgreSQL needs elbow room beyond the payload itself.
ExtraDiskSpaceRequired=524288000

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
; The database lives OUTSIDE Program Files, deliberately. Program Files is
; read-only for non-elevated processes, and an uninstall that removed {app}
; would take the client's books with it. ProgramData survives uninstall unless
; the user explicitly asks otherwise (see UninstallDelete / the uninstall prompt).
Name: "{commonappdata}\TallyBridge\data"
Name: "{app}\logs"

[Code]
var
  SeedPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  SeedPage := CreateInputQueryPage(wpSelectDir,
    'Copy existing data',
    'Optional: fill the new database from an existing Supabase project.',
    'Leave both boxes empty to install an empty database. You can copy the data ' +
    'later by running copy-live-to-local.mjs.' + #13#10#13#10 +
    'The service key is used once, during this install, and is not stored.');
  SeedPage.Add('Project URL (https://xxxx.supabase.co):', False);
  SeedPage.Add('service_role key:', False);
end;

function SeedUrl(Param: String): String;
begin
  Result := Trim(SeedPage.Values[0]);
end;

function SeedKey(Param: String): String;
begin
  Result := Trim(SeedPage.Values[1]);
end;

function DataDir(Param: String): String;
begin
  Result := ExpandConstant('{commonappdata}\TallyBridge\data');
end;

[Run]
; One PowerShell call does the whole install: generate secrets, create the
; cluster, apply the schema, register and start the four services, optionally
; copy data, then verify. Keeping it in a script rather than a chain of [Run]
; entries means the same sequence is testable outside the installer.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\post-install.ps1"" -InstallDir ""{app}"" -DataDir ""{code:DataDir}"" -LiveUrl ""{code:SeedUrl}"" -LiveKey ""{code:SeedKey}"""; \
  StatusMsg: "Setting up the database (this takes a few minutes)..."; \
  Flags: runhidden waituntilterminated

[UninstallRun]
; Stop and deregister the services BEFORE Inno deletes {app}. Skipped otherwise,
; the service entries survive as orphans pointing at a path that no longer
; exists, and Windows reports them as failed on every boot.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\pre-uninstall.ps1"" -InstallDir ""{app}"""; \
  RunOnceId: "RemoveTallyBridgeServices"; \
  Flags: runhidden waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}\config"
Type: filesandordirs; Name: "{app}\logs"
; NOTE: {commonappdata}\TallyBridge\data is deliberately NOT listed. After
; cutover that directory is the client's entire books, and an uninstall must not
; be able to destroy it by accident. pre-uninstall.ps1 prints where it is.
