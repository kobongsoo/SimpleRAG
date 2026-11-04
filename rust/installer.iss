; ------------------------------------------------------------------
; MoAISearch 설치 파일 (Inno Setup 6)
; => 앱이 exe 옆에 인덱스 · ini 를 쓰므로 사용자 폴더에 관리자 권한 없이 설치한다.
;    파일은 하나씩 지정한다 — dist-rust 를 통째로 묶으면 개발 PC 의 인덱스(문서 내용) · ini(폴더 경로)가 따라간다.
;    build_installer.ps1 이 부른다. 인자: /DAppVersion=<버전> /DDist=<dist-rust> /DIniTemplate=<빈 ini>
; ------------------------------------------------------------------
#define AppName "MoAI Search"
#define AppExe "MoAISearch.exe"

[Setup]
AppId={{7E0C6F0A-3B1D-4C55-9A61-5D2B7C1E4F10}
AppName={#AppName}
AppVersion={#AppVersion}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
DefaultDirName={localappdata}\Programs\MoAISearch
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#Dist}
OutputBaseFilename=MoAISearch_Setup_{#AppVersion}
SetupIconFile=ragsearchbox\RAGSearchBox.ico
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
; 실행 중이면 설치 · 제거 전에 끄라고 안내한다 — main.rs 의 MUTEX_NAME 과 같다
AppMutex=Local\RAGSearchBox-rs
CloseApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로 가기 만들기"
Name: "autorun"; Description: "Windows 로그인 때 자동 시작"; Flags: unchecked

[Files]
Source: "{#Dist}\MoAISearch.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\simplerag-rs.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\onnxruntime.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\onnxruntime_providers_shared.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\pdfium.dll"; DestDir: "{app}"; Flags: ignoreversion
; Visual C++ 런타임 — 설치되지 않은 PC 에서도 뜨도록 exe 옆에 둔다
Source: "{#Dist}\vcruntime140.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\vcruntime140_1.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\msvcp140.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\msvcp140_1.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\vcomp140.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\LICENSES_MODELS.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\LICENSES_THIRD_PARTY.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\synonyms.default.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\people.default.yaml"; DestDir: "{app}"; Flags: ignoreversion
; 사용 설명서 — [?] → 사용 설명서 열기 (build_dist.ps1 이 doc 폴더의 최신 설명서를 담아 둔다)
Source: "{#Dist}\doc\*"; DestDir: "{app}\doc"; Flags: ignoreversion skipifsourcedoesntexist
; 설정은 처음 설치할 때만 넣는다 — 업데이트가 사용자의 폴더 · 설정을 덮어쓰지 않게
Source: "{#Dist}\config.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "{#IniTemplate}"; DestDir: "{app}"; DestName: "RAGSearchBox.ini"; Flags: onlyifdoesntexist
Source: "{#Dist}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs
; ONNX 모델 · tokenizer 는 20~80% 줄어들어 압축한다
Source: "{#Dist}\models\e5-small-ko\*"; DestDir: "{app}\models\e5-small-ko"; Flags: ignoreversion
Source: "{#Dist}\models\bge-reranker-base-int8\*"; DestDir: "{app}\models\bge-reranker-base-int8"; Flags: ignoreversion
; GGUF 는 12% 밖에 줄지 않아 시간만 든다. 정밀 모드의 Qwen3-4B 는 installer_precision.iss 로 따로 설치한다
Source: "{#Dist}\models\Qwen3-1.7B-Q4_K_M.gguf"; DestDir: "{app}\models"; Flags: ignoreversion nocompression

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--autorun on --quiet"; Flags: runhidden; Tasks: autorun
Filename: "{app}\{#AppExe}"; Description: "MoAI Search 실행"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 파일을 지우기 전에 앱이 남긴 흔적을 되돌린다
Filename: "{app}\{#AppExe}"; Parameters: "--autorun off --quiet"; Flags: runhidden; RunOnceId: "AutorunOff"
Filename: "{app}\{#AppExe}"; Parameters: "--folder-icons restore"; Flags: runhidden; RunOnceId: "IconsRestore"

[UninstallDelete]
; 인덱스 · 사용자 사전은 앱이 만든 파일이라 제거 목록에 없다
Type: filesandordirs; Name: "{app}"

[Code]
//------------------------------------------------------------------
// WebView2 런타임이 있나 — 화면이 WebView2 로 그려지므로 없으면 앱이 뜨지 않는다
//------------------------------------------------------------------
function HasWebView2(): Boolean;
var
  V, Key: String;
begin
  Key := 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + Key, 'pv', V)
    or RegQueryStringValue(HKCU, 'Software\' + Key, 'pv', V);
  if Result then
    Result := (V <> '') and (V <> '0.0.0.0');
end;

//------------------------------------------------------------------
// 설치 폴더의 워커 · 인덱싱 프로세스 끄기 — 앱이 비정상으로 끝나 혼자 남으면 파일이 잠겨 지워지지 않는다
//=> 이름만으로 끄면 다른 폴더의 simplerag-rs 까지 꺼지므로 경로로 고른다
//------------------------------------------------------------------
procedure StopWorkers();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -NonInteractive -Command "Get-Process simplerag-rs -ErrorAction SilentlyContinue | Where-Object {$_.Path -like ''' +
       ExpandConstant('{app}') + '\*''} | Stop-Process -Force"',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopWorkers();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopWorkers();
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not HasWebView2() then
    MsgBox('Microsoft Edge WebView2 런타임이 없습니다.' + #13#10 +
           '설치 후 Microsoft 누리집에서 WebView2 Runtime 을 받아 설치해 주세요.', mbInformation, MB_OK);
end;
