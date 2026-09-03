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
Source: "..\installer\*";    DestDir: "{app}\installer";    Flags: recursesubdirs createallsubdirs ignoreversion
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
    Comment: "Start the live narrator"

Name: "{group}\{#AppName} (replay, no MetaTrader needed)"; \
    Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\launch.ps1"" -Replay"; \
    WorkingDir: "{app}"; \
    Comment: "Run on recorded bars, for trying it out without a broker account"

Name: "{group}\{#AppName} Setup"; \
    Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\setup.ps1"""; \
    WorkingDir: "{app}"; \
    Comment: "Re-run or repair the requirements install"

Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

Name: "{autodesktop}\{#AppName}"; \
    Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\launch.ps1"""; \
    WorkingDir: "{app}"; \
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
