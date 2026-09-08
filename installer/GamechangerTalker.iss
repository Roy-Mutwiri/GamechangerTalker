; Gamechanger Talker -- Windows installer.
;
; Builds GamechangerTalkerSetup.exe. The installer itself is small: it ships
; the project source and then runs installer\setup.ps1, which downloads the
; heavy dependencies (PyTorch, the speech model, two local AI models) on the
; target machine. Bundling those instead would make a 15 GB installer that goes
; stale the moment any of them is updated.
;
; Installs per-user, under LocalAppData, so it needs no administrator rights --
; which is also what keeps pip and the virtual environment working without
; elevation later.
;
; Build:  powershell -ExecutionPolicy Bypass -File installer\build.ps1

#define AppName        "Gamechanger Talker"
#define AppShortName   "GamechangerTalker"
#define AppVersion     "1.0.0"
#define AppPublisher   "Roy Mutwiri"
#define AppURL         "https://github.com/Roy-Mutwiri/GamechangerTalker"
#define AppIcon        "GamechangerTalker.ico"

[Setup]
AppId={{8E4C1F2A-6B3D-4A7E-9C15-2F8D6A1B4E77}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={localappdata}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist
OutputBaseFilename={#AppShortName}Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
SetupLogging=yes

; The icon, in the four places Windows looks for one. Without these the
; installer carries Inno's default, Add/Remove Programs shows a blank
; square, and every shortcut below shows the PowerShell icon -- because
; that is genuinely what they launch.
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\installer\{#AppIcon}

; What right-click -> Properties -> Details shows on the setup exe. Blank
; here is what makes a download look like something a browser should warn
; about, and it is four lines to fix.
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
VersionInfoCompany={#AppPublisher}
VersionInfoCopyright=(c) {#AppPublisher}
AppCopyright=(c) {#AppPublisher}

; The narrator needs a CUDA build of PyTorch and a modern console; there is
; no version of this that works on Windows 8.
MinVersion=10.0

; The files copied here are small. This is the virtual environment that
; setup.ps1 builds afterwards -- roughly 4 GB of PyTorch and its
; dependencies -- declared so Inno's own space check knows about it rather
; than passing a machine that will run out an hour later. The models are
; larger still and live outside {app}; the check in [Code] covers those.
ExtraDiskSpaceRequired=4294967296

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "runsetup"; Description: "Download and install requirements now (about 15 GB, one time)"; GroupDescription: "Setup:"

[Files]
; The application itself. Excludes are the things that must never travel:
; a virtual environment built against another machine's paths, the git
; history, caches, logs, and the setup stamp (which would make a fresh
; install think it had already downloaded everything).
Source: "..\narrator\*";     DestDir: "{app}\narrator";     Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "__pycache__,*.pyc"
Source: "..\templates\*";    DestDir: "{app}\templates";    Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\tools\*";        DestDir: "{app}\tools";        Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "__pycache__,*.pyc"
Source: "..\avatars\*";      DestDir: "{app}\avatars";      Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\warudo\*";       DestDir: "{app}\warudo";       Flags: recursesubdirs createallsubdirs ignoreversion
; Carries setup.ps1, launch.ps1 and the icon that UninstallDisplayIcon and
; every shortcut point at, so all three keep working after the setup exe is
; deleted. make_icon.py and the .iss are build-time things and stay behind:
; nothing on the target machine can use them, and an installed copy of the
; installer's own source invites somebody to edit the wrong file.
Source: "..\installer\*";    DestDir: "{app}\installer";    Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "make_icon.py,*.iss,build.ps1,__pycache__,*.pyc"
Source: "..\config.toml";    DestDir: "{app}";              Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}";            Flags: ignoreversion
Source: "..\pyproject.toml"; DestDir: "{app}";              Flags: ignoreversion
Source: "..\run.ps1";        DestDir: "{app}";              Flags: ignoreversion
Source: "..\run-forever.ps1"; DestDir: "{app}";             Flags: ignoreversion
Source: "..\README.md";      DestDir: "{app}";              Flags: ignoreversion
Source: "..\WARUDO_SETUP.md"; DestDir: "{app}";             Flags: ignoreversion
Source: "..\ARCHITECTURE.md"; DestDir: "{app}";             Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; \
    Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\launch.ps1"""; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\installer\{#AppIcon}"; \
    Comment: "Start the live narrator"

Name: "{group}\{#AppName} (replay, no MetaTrader needed)"; \
    Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\launch.ps1"" -Replay"; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\installer\{#AppIcon}"; \
    Comment: "Run on recorded bars, for trying it out without a broker account"

Name: "{group}\{#AppName} Setup"; \
    Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\setup.ps1"""; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\installer\{#AppIcon}"; \
    Comment: "Re-run or repair the requirements install"

Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

Name: "{autodesktop}\{#AppName}"; \
    Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\launch.ps1"""; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\installer\{#AppIcon}"; \
    Tasks: desktopicon

[Run]
; Visible on purpose. This step downloads about 15 GB and can take a long time
; on a slow line; hiding it behind a progress bar with no detail is how an
; installer ends up looking hung and getting killed half way through.
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\setup.ps1"" -NoPause"; \
    WorkingDir: "{app}"; \
    StatusMsg: "Downloading and installing requirements (this takes a while)..."; \
    Flags: waituntilterminated; \
    Tasks: runsetup

Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\launch.ps1"""; \
    WorkingDir: "{app}"; \
    Description: "Start {#AppName} now"; \
    Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; Built on this machine, not shipped, so the uninstaller owns them. Leaving a
; 5 GB virtual environment behind after an uninstall is its own kind of rude.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\cache"
Type: files;          Name: "{app}\.setup-complete"
Type: filesandordirs; Name: "{app}\narrator\__pycache__"
Type: filesandordirs; Name: "{app}\tools\__pycache__"

[Messages]
WelcomeLabel2=This will install [name] on your computer.%n%nIt needs PyTorch, a speech model and two local AI models -- about 15 GB in total, downloaded once after the files are copied. A NVIDIA graphics card is strongly recommended.

; The last page is the one people actually read, because it is the one
; standing between them and the thing working. The default says "Setup has
; finished installing", which is true and useless: the narrator reads prices
; out of MetaTrader and will sit there saying nothing until somebody logs into
; it. These are the same three steps setup.ps1 prints, in the same order.
FinishedHeadingLabel=[name] is installed
FinishedLabel=Before the first run:%n%n      1.  Open MetaTrader 5 and log in. A free demo account is fine.%n      2.  Leave it running -- the narrator reads its prices.%n      3.  Start [name] from the Start Menu or the desktop.%n%nWithout MetaTrader it still runs: the replay shortcut speaks over recorded bars, with no broker account needed.
ClickFinish=Click Finish to close.

[Code]
// A guard on the 15 GB that setup.ps1 downloads AFTER this installer has
// finished and closed.
//
// Inno's own disk check only knows about ExtraDiskSpaceRequired, which covers
// the virtual environment under the install directory and nothing else: the
// language models go to %USERPROFILE%\.ollama and the installers stage through
// %TEMP%, neither of which it can see. So a machine with 6 GB free passes
// every check this installer makes and then dies an hour into a download,
// which is the worst possible moment to find out.
//
// It warns rather than refuses. An operator who already has the models -- a
// reinstall, a second copy -- genuinely does not need the space, and an
// installer that will not let you past a wrong guess is worse than one that
// tells you what it thinks and then believes you.
//
// Line comments, not brace comments: a Pascal { } comment is ended by the
// first } inside it, and this file is full of {app} and {group}.

const
  WantedGB = 20;

function FreeGigabytes(const Path: String): Int64;
var
  Free, Total: Int64;
begin
  Result := -1;
  if GetSpaceOnDisk64(ExtractFileDrive(Path), Free, Total) then
    Result := Free div 1073741824;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  OnApp, OnProfile: Int64;
  Message: String;
begin
  Result := True;
  if CurPageID <> wpSelectDir then
    Exit;

  OnApp := FreeGigabytes(WizardDirValue);
  OnProfile := FreeGigabytes(GetEnv('USERPROFILE'));
  if (OnApp < 0) or (OnApp >= WantedGB) then
    Exit;

  Message :=
    'There is ' + IntToStr(OnApp) + ' GB free on ' + ExtractFileDrive(WizardDirValue) +
    ', and the download after this installer needs about ' + IntToStr(WantedGB) + ' GB.'#13#10#13#10 +
    'The files this installer copies will fit. What will not fit is PyTorch, the ' +
    'speech model and the two language models, which are fetched afterwards and ' +
    'will fail part way through.'#13#10#13#10;
  if (OnProfile >= 0) and (ExtractFileDrive(GetEnv('USERPROFILE')) <> ExtractFileDrive(WizardDirValue)) then
    Message := Message + 'The language models go to your user profile on ' +
      ExtractFileDrive(GetEnv('USERPROFILE')) + ', which has ' + IntToStr(OnProfile) +
      ' GB free.'#13#10#13#10;
  Message := Message +
    'Continue anyway? Choose No if you would rather free some space first, or ' +
    'if you already have the models and know they are there.';

  Result := MsgBox(Message, mbConfirmation, MB_YESNO) = IDYES;
end;
