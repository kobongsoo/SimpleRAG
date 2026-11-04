; ------------------------------------------------------------------
; MoAISearch 정밀 모드 모델 추가 설치 (Inno Setup 6)
; => 2.5GB 인 Qwen3-4B 를 기본 설치 파일에서 뺐으므로, 정밀 모드를 쓸 사용자만 이 파일로 추가한다.
;    기본 프로그램이 설치된 폴더를 찾아 models 에 넣는다. 제거는 기본 프로그램을 지울 때 함께 된다.
;    build_installer.ps1 이 부른다. 인자: /DAppVersion=<버전> /DDist=<dist-rust>
; ------------------------------------------------------------------
#define ModelFile "Qwen3-4B-Q4_K_M.gguf"
; installer.iss 의 AppId 뒤에 Inno 가 _is1 을 붙인 제거 정보 키
#define MainKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E0C6F0A-3B1D-4C55-9A61-5D2B7C1E4F10}_is1"

[Setup]
AppId={{2B7D9C41-6E0F-4A3B-8F52-1C9E7A4D6B23}
AppName=MoAI Search 정밀 모드 모델
AppVersion={#AppVersion}
DefaultDirName={code:MainDir}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
UsePreviousAppDir=no
; 기본 프로그램의 설치 폴더를 지울 때 모델도 함께 지워지므로 따로 제거 정보를 남기지 않는다
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#Dist}
OutputBaseFilename=MoAISearch_PrecisionModel_Setup_{#AppVersion}
SetupIconFile=ragsearchbox\RAGSearchBox.ico
Compression=none
; GitHub 릴리스는 첨부 파일 하나가 2GB 를 넘지 못한다 — 모델(2.5GB)을 1.9GB 조각(.bin)으로 나눈다.
;   설치 파일(.exe)과 조각들을 같은 폴더에 두고 .exe 를 실행한다
DiskSpanning=yes
DiskSliceSize=1900000000
WizardStyle=modern
; 쓰는 중인 모델 파일을 덮어쓰지 않게 앱을 끈 뒤 설치한다 — main.rs 의 MUTEX_NAME 과 같다
AppMutex=Local\RAGSearchBox-rs
CloseApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Files]
Source: "{#Dist}\models\{#ModelFile}"; DestDir: "{app}\models"; Flags: ignoreversion nocompression

[Code]
//------------------------------------------------------------------
// 기본 프로그램 설치 폴더 — 없으면 빈 글
//------------------------------------------------------------------
function FindMainDir(): String;
begin
  if not RegQueryStringValue(HKCU, '{#MainKey}', 'InstallLocation', Result) then
    Result := '';
end;

function MainDir(Param: String): String;
begin
  Result := FindMainDir();
end;

function InitializeSetup(): Boolean;
begin
  Result := FindMainDir() <> '';
  if not Result then
    MsgBox('MoAI Search 가 설치되어 있지 않습니다.' + #13#10 + '먼저 MoAI Search 를 설치해 주세요.', mbError, MB_OK);
end;
