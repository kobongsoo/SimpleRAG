; ------------------------------------------------------------------
; MoAI Search 답변 모델 추가 설치 (Inno Setup 6) — 모델 하나에 설치 파일 하나 (모델 받기 설계서 §7-2)
; => 설정 > 모델의 [받기] 가 이 파일을 GitHub 릴리스에서 받아 조용히(/VERYSILENT) 실행한다.
;    사용자가 배포 페이지에서 직접 받아 실행해도 된다(.exe 와 조각 .bin 을 같은 폴더에 두고 .exe 실행).
;    기본 프로그램이 설치된 폴더를 레지스트리에서 찾아 models 에 넣는다. 지우기는 설정 > 모델 [삭제] 나 기본 프로그램 제거로.
;    · 앱이 켜진 채로 앱이 실행하므로 AppMutex 를 두지 않는다(새 모델 파일은 아직 아무도 쓰지 않는다)
;    · 넣는 것: models\<모델>.gguf · models\<별칭>.model.json(워커가 읽는 설명) · licenses\models\<별칭>.txt
;    build_model_installers.py 가 부른다. 인자:
;      /DAlias= /DModelFile= /DModelName= /DRev= /DSrc=<gguf 폴더> /DSidecar=<model.json> /DLicense=<라이선스 글> /DOut=<출력 폴더>
;      /DSplit=1 이면 1.9GB 조각(.bin)으로 나눈다 — GitHub 릴리스 첨부 한도 2GB
;      /DRuntime=<llama.cpp 판 폴더> /DRuntimeName=<이름> 이면 runtime\<이름> 에 그 DLL 들도 넣는다(없으면 안 넣음)
; ------------------------------------------------------------------
; installer.iss 의 AppId 뒤에 Inno 가 _is1 을 붙인 제거 정보 키
#define MainKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E0C6F0A-3B1D-4C55-9A61-5D2B7C1E4F10}_is1"
#ifndef Split
  #define Split "0"
#endif

[Setup]
AppId=MoAISearch.Model.{#Alias}
AppName=MoAI Search 모델 — {#ModelName}
AppVersion={#Rev}
DefaultDirName={code:MainDir}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
UsePreviousAppDir=no
; 기본 프로그램 폴더를 지울 때 함께 지워지므로 따로 제거 정보를 남기지 않는다
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#Out}
OutputBaseFilename=MoAISearch_Model_{#Alias}_r{#Rev}
SetupIconFile=ragsearchbox\RAGSearchBox.ico
; GGUF 는 압축해도 12% 밖에 줄지 않아 시간만 든다
Compression=none
#if Split == "1"
DiskSpanning=yes
DiskSliceSize=1900000000
#endif
WizardStyle=modern
CloseApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Files]
Source: "{#Src}\{#ModelFile}"; DestDir: "{app}\models"; Flags: ignoreversion nocompression
Source: "{#Sidecar}"; DestDir: "{app}\models"; DestName: "{#Alias}.model.json"; Flags: ignoreversion
Source: "{#License}"; DestDir: "{app}\licenses\models"; DestName: "{#Alias}.txt"; Flags: ignoreversion
#ifdef Runtime
; 이 모델만 쓰는 llama.cpp 판(예: Kanana-2 의 kanana2tiny 패치판) — 워커가 설명 파일의 runtime 을 보고 이 폴더의 DLL 을 쓴다
Source: "{#Runtime}\*"; DestDir: "{app}\runtime\{#RuntimeName}"; Flags: ignoreversion
#endif

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
