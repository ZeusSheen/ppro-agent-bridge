; Premiere Agent Bridge - the Windows installer (Inno Setup 6.5 or newer: 6.5 ships the Simplified Chinese messages).
;
; Do not compile this file where it lies. `python scripts/build_installer.py <release export folder>` stages a copy of
; it next to the files it installs and two generated includes, then runs ISCC on that copy:
;   <stage>\PremiereAgentBridge.iss       this file
;   <stage>\generated.iss                 AppVersion (from the export's VERSION), PythonVersion, ProjectUrl, ...
;   <stage>\generated_uninstall.iss       [UninstallDelete] lines for the __pycache__ folders Python writes later
;   <stage>\app\                          the release export, plus python\ (the embeddable Python, checked against
;                                         installer\python.lock.json) and installed-files.txt (every file of app\
;                                         outside python\, for the upgrade clean-up in [Code])
;
; What it does: a per-user install without admin rights into %LOCALAPPDATA%\Programs\Premiere Agent Bridge (Inno's
; {userpf}); Start menu shortcuts (the bridge, and its setup window), an optional desktop shortcut and an optional
; start at login - the same "Premiere Agent Bridge.lnk" names, targets and icon as doctor.make_shortcut, so the setup
; window sees and toggles them; an uninstaller in Settings > Apps; the setup window (ppro_bridge.setup_ui) after
; install for what an installer cannot do (UXP Developer Tool, developer mode). Upgrades install over the old version
; in place (same AppId) and remove the files it no longer ships.
;
; Why %LOCALAPPDATA% is fine here although the bridge keeps its token and audit log out of it: the Claude desktop app
; is an MSIX package, and Windows redirects what its child processes WRITE under %LOCALAPPDATA% into a private copy.
; This folder holds only the program, which those processes READ (reads are not redirected); the token and the audit
; log stay in %USERPROFILE%\.ppro-agent-bridge (ppro_bridge/auth.py), which no packaged app sees a copy of. The one
; thing written under the program folder at run time is Python's __pycache__ (and dist\*.ccx from the install step):
; a private copy of a cache is harmless.
;
; Text files of this repository use LF line endings; Inno Setup reads UTF-8 without a BOM since 6.3.

#define AppName "Premiere Agent Bridge"
#define FileList "installed-files.txt"

#if !FileExists(AddBackslash(SourcePath) + "generated.iss")
  #error Compile the staged copy that scripts/build_installer.py writes (python scripts/build_installer.py <release export folder>), not installer\PremiereAgentBridge.iss itself.
#endif
#include "generated.iss"

#if Ver < EncodeVer(6, 5, 0, 0)
  #error Inno Setup 6.5 or newer is needed.
#endif

; The Chinese wizard texts: Inno Setup 6.7.1 ships no ChineseSimplified.isl (it is one of the unofficial translations,
; jrsoftware/issrc Files/Languages/Unofficial). The compiler's copy when it has one, else installer\ChineseSimplified.isl
; (copied next to this script by build_installer.py), else the English texts under a Chinese language name.
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
  #define ZhMessages "compiler:Languages\ChineseSimplified.isl"
  #define ZhFallback 0
#elif FileExists(AddBackslash(SourcePath) + "ChineseSimplified.isl")
  #define ZhMessages "ChineseSimplified.isl"
  #define ZhFallback 0
#else
  #pragma warning "No ChineseSimplified.isl: the Chinese wizard shows Inno Setup's English buttons and pages; our own texts stay Chinese."
  #define ZhMessages "compiler:Default.isl"
  #define ZhFallback 1
#endif

[Setup]
; Never change the AppId: Windows and upgrades know the installed bridge by it.
AppId={{2FEB9350-3CFC-41E9-B25C-3CF297854713}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Premiere Agent Bridge contributors
#if ProjectUrl != ""
AppPublisherURL={#ProjectUrl}
AppSupportURL={#ProjectUrl}/issues
AppUpdatesURL={#ProjectUrl}/releases
#endif
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
VersionInfoCopyright=MIT License, Premiere Agent Bridge contributors
; Per user, no admin rights, no UAC prompt.
PrivilegesRequired=lowest
DefaultDirName={userpf}\{#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes
; The bundled Python is amd64 (it also runs under x64 emulation on Windows on Arm).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=app\ppro_bridge\bridge.ico
UninstallDisplayIcon={app}\ppro_bridge\bridge.ico
UninstallDisplayName={#AppName}
OutputBaseFilename=PremiereAgentBridge-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
; A running bridge holds python.exe, its DLLs and .pyd files open: the Restart Manager offers to close it first, and
; it is not restarted afterwards (start it from the shortcut or the setup window).
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
ShowLanguageDialog=auto
LanguageDetectionMethod=uilanguage
ChangesAssociations=no
ChangesEnvironment=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "zh"; MessagesFile: "{#ZhMessages}"

#if ZhFallback
[Messages]
zh.LanguageName=简体中文
zh.LanguageID=$0804
zh.LanguageCodePage=0
#endif

[CustomMessages]
en.GroupStartup=Starting the bridge:
zh.GroupStartup=启动桥：
en.TaskAutostart=Start the bridge when I log in to Windows (minimized)
zh.TaskAutostart=登录 Windows 时自动启动桥（最小化）
en.ShortcutComment=Start the Premiere Agent Bridge (keep its window open while you edit)
zh.ShortcutComment=启动 Premiere Agent Bridge（剪辑时保持它的窗口开着）
en.SetupComment=Check this computer and finish setting up the Premiere Agent Bridge
zh.SetupComment=检查这台电脑，完成 Premiere Agent Bridge 的设置
en.PackagingPanel=Packaging the Premiere panel ...
zh.PackagingPanel=正在打包 Premiere 面板……
en.RunSetupWindow=Open the setup window to finish (UXP Developer Tool, developer mode)
zh.RunSetupWindow=打开设置窗口，完成剩下的步骤（UXP Developer Tool、开发者模式）
en.GroupUpdates=Updates:
zh.GroupUpdates=更新：
en.TaskUpdateCheck=Check GitHub once a day for a new version of the bridge (one request to api.github.com; nothing about you or your projects is sent)
zh.TaskUpdateCheck=每天到 GitHub 检查一次桥有没有新版本（向 api.github.com 发一次请求，不发送任何关于你或你的项目的信息）
en.AskDeleteData=Also delete your bridge data?%n%n%1%n%nIt holds the pairing token and the audit log (what the bridge did, kept 30 days). Another copy of the bridge on this computer makes a new token when it starts.%n%nChoose No to keep it (recommended if you plan to install the bridge again).
zh.AskDeleteData=同时删除桥的数据吗？%n%n%1%n%n里面是配对令牌和审计日志（桥做过的操作，保留 30 天）。这台电脑上的另一份桥启动时会生成新的令牌。%n%n选“否”保留（以后还要装桥的话，建议保留）。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:TaskAutostart}"; GroupDescription: "{cm:GroupStartup}"; Flags: unchecked
; The bridge's one outbound request (ppro_bridge/update.py), on by default as the user chose; unticked, the [Run]
; entry below writes settings.json {"updateCheck": false}. Ticked, nothing is written: a later "off" in settings.json
; stays off.
Name: "updatecheck"; Description: "{cm:TaskUpdateCheck}"; GroupDescription: "{cm:GroupUpdates}"

[InstallDelete]
; A fresh bundled Python on every install and upgrade: the runtime and its packages are pinned and hash-checked at
; build time, and nothing from an older pin is mixed in. Packages installed later from the setup window (Pillow,
; faster-whisper) go with it; the setup window, which opens after the upgrade, offers them again. Only over an
; install of ours (its file list is there): a folder chosen by hand that happens to hold a python folder keeps it.
Type: filesandordirs; Name: "{app}\python"; Check: OurInstallIsHere

[Files]
Source: "app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The new file list once more, for [Code] to compare with the old one before anything is copied.
Source: "app\{#FileList}"; Flags: dontcopy

[Icons]
; The same names, targets, working folder and icon as doctor.make_shortcut (the setup window's shortcut buttons).
Name: "{userprograms}\{#AppName}"; Filename: "{app}\start_bridge.bat"; WorkingDir: "{app}"; IconFilename: "{app}\ppro_bridge\bridge.ico"; Comment: "{cm:ShortcutComment}"
Name: "{userprograms}\{#AppName} Setup"; Filename: "{app}\python\pythonw.exe"; Parameters: "-I -m ppro_bridge.setup_ui"; WorkingDir: "{app}"; IconFilename: "{app}\ppro_bridge\bridge.ico"; Comment: "{cm:SetupComment}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\start_bridge.bat"; WorkingDir: "{app}"; IconFilename: "{app}\ppro_bridge\bridge.ico"; Comment: "{cm:ShortcutComment}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\start_bridge.bat"; WorkingDir: "{app}"; IconFilename: "{app}\ppro_bridge\bridge.ico"; Comment: "{cm:ShortcutComment}"; Flags: runminimized; Tasks: autostart

[Run]
; The panel as a .ccx for Creative Cloud's plugin installer, built here because its launcher.json must name this
; install's start_bridge.bat (doctor prints the install command; nothing installs it). A failure changes nothing.
Filename: "{app}\python\python.exe"; Parameters: "-I ""{app}\scripts\build_ccx.py"""; WorkingDir: "{app}"; StatusMsg: "{cm:PackagingPanel}"; Flags: runhidden waituntilterminated
; Update check unticked: settings.json {"updateCheck": false} through the bridge's own writer (it keeps every other
; key and never overwrites a settings.json it cannot read).
Filename: "{app}\python\python.exe"; Parameters: "-I -c ""from ppro_bridge import update; update.write_setting('updateCheck', False)"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; Tasks: not updatecheck
Filename: "{app}\python\pythonw.exe"; Parameters: "-I -m ppro_bridge.setup_ui"; WorkingDir: "{app}"; Description: "{cm:RunSetupWindow}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; What the bridge writes into its own folder after install. Work the CLI wrote under cli\ (frames, transcripts,
; plans) is not touched, so the folder stays when there is any. python\ and dist\ go whole only when the folder was
; ours before this install (new, empty, or an install of ours): installed into a folder of the user's own, a python\
; or dist\ of theirs stays (the files this installer put there are still removed one by one).
Type: filesandordirs; Name: "{app}\python"; Check: AppFolderWasOurs
Type: filesandordirs; Name: "{app}\dist"; Check: AppFolderWasOurs
#include "generated_uninstall.iss"

[Code]
var
  DeleteBridgeData: Boolean;
  AppFolderOurs: Boolean;

// An earlier install of the bridge is in {app} (its file list is there).
function OurInstallIsHere(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\{#FileList}'));
end;

// A line of installed-files.txt that may be deleted under {app}: relative, no drive, no "..", backslashes only.
function SafeRelativePath(const Path: String): Boolean;
begin
  Result := (Path <> '') and (Pos(':', Path) = 0) and (Pos('..', Path) = 0) and (Pos('/', Path) = 0) and
    (Copy(Path, 1, 1) <> '\') and (Copy(Path, 1, 1) <> '#');
end;

// Upgrade in place: delete the files the previous version installed that this one no longer ships. A leftover
// plugin\lib\*.js would change the panel's code hash, and ping would call every panel stale.
procedure RemoveDroppedFiles();
var
  OldList, NewList, NewSet: TStringList;
  I: Integer;
  Name: String;
begin
  if not OurInstallIsHere() then
    Exit;
  OldList := TStringList.Create;
  NewList := TStringList.Create;
  NewSet := TStringList.Create;
  try
    OldList.LoadFromFile(ExpandConstant('{app}\{#FileList}'));
    ExtractTemporaryFile('{#FileList}');
    NewList.LoadFromFile(ExpandConstant('{tmp}\{#FileList}'));
    NewSet.Sorted := True;
    for I := 0 to NewList.Count - 1 do
      NewSet.Add(Lowercase(Trim(NewList.Strings[I])));
    for I := 0 to OldList.Count - 1 do
    begin
      Name := Trim(OldList.Strings[I]);
      if SafeRelativePath(Name) and (NewSet.IndexOf(Lowercase(Name)) < 0) then
        DeleteFile(ExpandConstant('{app}\') + Name);
    end;
  finally
    OldList.Free;
    NewList.Free;
    NewSet.Free;
  end;
end;

// {app} holds nothing yet (missing, or an empty folder).
function AppFolderEmpty(): Boolean;
var
  FindRec: TFindRec;
begin
  Result := True;
  if FindFirst(ExpandConstant('{app}\*'), FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
          Result := False;
      until (not Result) or (not FindNext(FindRec));
    finally
      FindClose(FindRec);
    end;
  end;
end;

// Asked by [UninstallDelete] while it is recorded: whether {app} was ours before this install (see ssInstall).
function AppFolderWasOurs(): Boolean;
begin
  Result := AppFolderOurs;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    // Before any file is copied: a new or empty folder, or an earlier install of ours.
    AppFolderOurs := AppFolderEmpty() or OurInstallIsHere();
    RemoveDroppedFiles();
  end;
end;

// The target of a .lnk ('' when it cannot be read).
function ShortcutTarget(const Link: String): String;
var
  Shell, Shortcut: Variant;
begin
  Result := '';
  try
    Shell := CreateOleObject('WScript.Shell');
    Shortcut := Shell.CreateShortcut(Link);
    Result := Shortcut.TargetPath;
  except
    Result := '';
  end;
end;

// Shortcuts the setup window made (doctor.make_shortcut) are not in the uninstall log: delete one only when it
// starts something inside this install, never a zip copy's.
procedure RemoveShortcutIfOurs(const Link: String);
var
  App, Target: String;
begin
  if not FileExists(Link) then
    Exit;
  App := Lowercase(AddBackslash(ExpandConstant('{app}')));
  Target := Lowercase(ShortcutTarget(Link));
  if (Target <> '') and (Copy(Target, 1, Length(App)) = App) then
    DeleteFile(Link);
end;

// %USERPROFILE%\.ppro-agent-bridge, or '' when USERPROFILE is not set.
function BridgeDataDir(): String;
begin
  Result := GetEnv('USERPROFILE');
  if Result <> '' then
    Result := AddBackslash(Result) + '.ppro-agent-bridge';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveShortcutIfOurs(ExpandConstant('{userdesktop}\{#AppName}.lnk'));
    RemoveShortcutIfOurs(ExpandConstant('{userprograms}\{#AppName}.lnk'));
    RemoveShortcutIfOurs(ExpandConstant('{userstartup}\{#AppName}.lnk'));
    // The token and the audit log stay unless the user says otherwise; No is the default button, and a silent
    // uninstall keeps them.
    DeleteBridgeData := False;
    Data := BridgeDataDir();
    if (Data <> '') and DirExists(Data) and not UninstallSilent() then
      DeleteBridgeData := MsgBox(FmtMessage(CustomMessage('AskDeleteData'), [Data]), mbConfirmation,
        MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end;
  if (CurUninstallStep = usPostUninstall) and DeleteBridgeData then
  begin
    Data := BridgeDataDir();
    if (Data <> '') and DirExists(Data) then
      DelTree(Data, True, True, True);
  end;
end;
