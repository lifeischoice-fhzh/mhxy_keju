; mhxy_keju 安装包脚本(Inno Setup 6)
;
; 平时不用手动碰这个文件:package.py 打完 dist/mhxy_keju 会自动调
;   iscc installer\installer.iss
; 单独重出安装包(不动 exe):.venv\Scripts\python.exe package.py --installer-only
;
; 安装策略:每用户安装,不需要管理员,默认装到
;   %LOCALAPPDATA%\Programs\mhxy_keju
; 程序把 config.json / data/ / voice_cache/ 都写在 exe 同目录(core.BASE_DIR
; 跟着 exe 走),所以必须装在用户可写目录,不能装 Program Files。
; 卸载只删安装时装进去的文件,配置/题库/收益/语音缓存留在原地。
#define MyAppName "mhxy_keju"
#define MyAppVersion "1.0.0"
#define MyAppExe MyAppName + ".exe"

[Setup]
AppId={{B92630C3-1F1E-4B0B-AEE0-A7B814F02A1B}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=mhxy_keju_v{#MyAppVersion}
SetupIconFile=app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExe}
CloseApplications=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务:"; Flags: checkedonce

[Files]
; 运行时产物不进安装包:config/日志/收益/事件/补题备份是每台机器各自的,
; 打包脚本会把开发机 dist 里的这些文件恢复回来(绿色版自用),压进安装包
; 会把开发机状态带给新用户 —— 剔除,首启由程序自建(config 默认值、
; data 目录、内置题库从 _internal 复制)
Source: "..\dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "config.json,keju.log,keju.log.old,data\events.jsonl,data\rewards.json,data\uncollected.jsonl,data\keju_questions.backup.json"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
