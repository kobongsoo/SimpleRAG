# ------------------------------------------------------------------
# 설치 파일 만들기
# => dist-rust 의 배포 파일로 설치 파일 2개를 dist-rust 에 만든다.
#    · MoAISearch_Setup_<버전>.exe               = 프로그램 + 기본 모델
#    · MoAISearch_PrecisionModel_Setup_<버전>.exe = 정밀 모드 모델(Qwen3-4B) 추가 설치
#    1) 개발 PC 폴더가 들어 있지 않은 빈 ini 를 만든다(저장소의 RAGSearchBox\RAGSearchBox.ini 기준)
#    2) Inno Setup 으로 묶는다 — 먼저 build_dist.ps1 을 실행해 둔다
#
# 사용: powershell -ExecutionPolicy Bypass -File rust\build_installer.ps1
# ------------------------------------------------------------------
$ErrorActionPreference = "Stop"
$rust = $PSScriptRoot
$root = Split-Path $rust -Parent
$dist = Join-Path $root "dist-rust"

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 이 없습니다 — winget install JRSoftware.InnoSetup" }

# 버전은 ragsearchbox 패키지 값을 따른다
$ver = (Select-String -Path (Join-Path $rust "ragsearchbox\Cargo.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value

# 빈 ini — 사용자가 첫 실행 때 인덱싱 폴더를 고른다
$tmp = Join-Path $rust "target\installer"
New-Item -ItemType Directory -Force $tmp | Out-Null
$ini = Join-Path $tmp "RAGSearchBox.ini"
(Get-Content (Join-Path $root "RAGSearchBox\RAGSearchBox.ini") -Encoding UTF8) |
    ForEach-Object { $_ -replace 'RAGSearchBox\.exe 옆 simplerag\.exe -> \.\.\\dist\\simplerag -> 개발용 소스', 'MoAISearch.exe 옆 simplerag-rs.exe' } |
    Where-Object { $_ -notmatch '--python-option u' } |
    Set-Content $ini -Encoding UTF8
if (Select-String -Path $ini -Pattern '^\s*Folders\s*=\s*\S' -Quiet) { throw "빈 ini 에 폴더 값이 있습니다: $ini" }

Push-Location $rust
& $iscc "/DAppVersion=$ver" "/DDist=$dist" "/DIniTemplate=$ini" "installer.iss"
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { throw "Inno Setup 실패 ($code)" }
# 정밀 모드 모델은 2.5GB 라 따로 묶는다
Push-Location $rust
& $iscc "/DAppVersion=$ver" "/DDist=$dist" "installer_precision.iss"
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { throw "Inno Setup 실패 - 정밀 모드 모델 ($code)" }
"완료 — $dist\MoAISearch_Setup_$ver.exe · MoAISearch_PrecisionModel_Setup_$ver.exe"
