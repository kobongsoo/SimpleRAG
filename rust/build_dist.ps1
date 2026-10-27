# ------------------------------------------------------------------
# Rust 판 빌드 + dist-rust 배포
# => 1) cargo build --release (simplerag-rs.exe, RAGSearchBox-rs.exe)
#    2) 저장소 루트 dist-rust\ 에 exe 를 넣는다
#    3) 처음이면 Python 배포본(dist\simplerag)에서 런타임 DLL 을 복사하고, 모델은 하드 링크로 건다
#       (같은 드라이브라 수 GB 모델이 두 벌 생기지 않는다), config.yaml·RAGSearchBox.ini 도 없을 때만 만든다
#    이미 있는 파일(설정·인덱스)은 덮어쓰지 않는다.
#
# 사용: powershell -ExecutionPolicy Bypass -File rust\build_dist.ps1
# ------------------------------------------------------------------
$ErrorActionPreference = "Stop"
$rust = $PSScriptRoot
$root = Split-Path $rust -Parent
$dist = Join-Path $root "dist-rust"
$py = Join-Path $root "dist\simplerag"

# 1) 빌드
Push-Location $rust
cargo build --release
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "cargo build 실패" }
Pop-Location

# 2) exe 배치
#    실행 중인 exe(트레이의 RAGSearchBox-rs, 그 워커 simplerag-rs)는 잠겨 있어 덮어쓸 수 없다.
#    그 하나만 건너뛰고 알린다 — 나머지 배치와 설정·모델 준비는 계속한다.
New-Item -ItemType Directory -Force $dist | Out-Null
$skipped = @()
foreach ($exe in "simplerag-rs.exe", "RAGSearchBox-rs.exe") {
    try {
        Copy-Item (Join-Path $rust "target\release\$exe") $dist -Force
        "배치: $exe"
    } catch [System.IO.IOException] {
        $skipped += $exe
        "건너뜀(실행 중이라 잠김): $exe"
    }
}

# 없을 때만 복사
function Copy-IfMissing($src, $dst) {
    if ((Test-Path $src) -and -not (Test-Path $dst)) { Copy-Item $src $dst; "복사: $dst" }
}
# 없을 때만 하드 링크(실패하면 복사)
function Link-IfMissing($src, $dst) {
    if ((Test-Path $src) -and -not (Test-Path $dst)) {
        try { New-Item -ItemType HardLink -Path $dst -Target $src | Out-Null; "링크: $dst" }
        catch { Copy-Item $src $dst; "복사: $dst" }
    }
}

# 3) 런타임 — onnxruntime·pdfium·llama.cpp(CPU / Vulkan)
Copy-IfMissing "$py\_internal\onnxruntime\capi\onnxruntime.dll" "$dist\onnxruntime.dll"
Copy-IfMissing "$py\_internal\onnxruntime\capi\onnxruntime_providers_shared.dll" "$dist\onnxruntime_providers_shared.dll"
Copy-IfMissing "$py\_internal\pypdfium2_raw\pdfium.dll" "$dist\pdfium.dll"
foreach ($sub in "runtime\llama_cpu", "runtime\llama_vulkan") { New-Item -ItemType Directory -Force "$dist\$sub" | Out-Null }
Get-ChildItem "$py\_internal\llama_cpp\lib\*.dll" -ErrorAction SilentlyContinue | ForEach-Object { Copy-IfMissing $_.FullName "$dist\runtime\llama_cpu\$($_.Name)" }
Get-ChildItem "$py\runtime\llama_vulkan\*" -ErrorAction SilentlyContinue | ForEach-Object { Copy-IfMissing $_.FullName "$dist\runtime\llama_vulkan\$($_.Name)" }
Copy-IfMissing "$py\runtime\llama_vulkan\VERSION.txt" "$dist\runtime\llama_cpu\VERSION.txt"

# 모델 — 하드 링크
foreach ($sub in "models", "models\e5-small-ko", "models\bge-reranker-base-int8") { New-Item -ItemType Directory -Force "$dist\$sub" | Out-Null }
Get-ChildItem "$py\models\*.gguf" -ErrorAction SilentlyContinue | ForEach-Object { Link-IfMissing $_.FullName "$dist\models\$($_.Name)" }
foreach ($m in "e5-small-ko", "bge-reranker-base-int8") {
    Get-ChildItem "$py\models\$m\*" -ErrorAction SilentlyContinue | ForEach-Object { Link-IfMissing $_.FullName "$dist\models\$m\$($_.Name)" }
}

# 기본 동의어 사전 — 배포 때마다 새것으로(사용자 사전 synonyms.yaml 은 건드리지 않는다)
Copy-Item (Join-Path $root "synonyms.default.yaml") $dist -Force
"복사: synonyms.default.yaml"

# 설정
Copy-IfMissing "$py\config.yaml" "$dist\config.yaml"
if (-not (Test-Path "$dist\RAGSearchBox.ini") -and (Test-Path "$py\RAGSearchBox.ini")) {
    (Get-Content "$py\RAGSearchBox.ini" -Encoding UTF8) |
        ForEach-Object { $_ -replace 'RAGSearchBox\.exe 옆 simplerag\.exe -> \.\.\\dist\\simplerag -> 개발용 소스', 'RAGSearchBox-rs.exe 옆 simplerag-rs.exe -> ..\dist-rust\simplerag-rs.exe' } |
        Where-Object { $_ -notmatch '--python-option u' } |
        Set-Content "$dist\RAGSearchBox.ini" -Encoding UTF8
    "만듦: $dist\RAGSearchBox.ini"
}
"완료 — $dist"
if ($skipped.Count -gt 0) {
    "⚠️ 실행 중이라 바꾸지 못한 exe: $($skipped -join ', ') — 트레이에서 RAGSearchBox-rs 를 끈 뒤 이 스크립트를 다시 실행하세요"
}
"인덱스가 없으면: dist-rust\simplerag-rs.exe import --from dist\simplerag  (Python 판 인덱스 가져오기, 다시 임베딩 없음)"
