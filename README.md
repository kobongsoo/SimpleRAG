# SimpleRAG · MoAISearch

로컬 Windows PC 에서 동작하는 **문서 검색 · 질의응답** 시스템.
인터넷 · 전용 GPU · 별도 서버 없이 돌아가고, 내장 GPU(iGPU)가 있으면 첫 실행 때 속도를 재서 빠를 때만 답변 생성에 쓴다.

```
문서 폴더 → [추출 · OCR → 청킹 → 임베딩 → 색인] → 질문 → 근거 3건(약 0.4초) → AI 요약(스트리밍)
```

검색된 **근거를 먼저 보여 주고** AI 요약이 뒤따르는 2단계 응답이 핵심이다. 생성 모델 정답률이 약 79% 라
사용자가 근거로 직접 확인할 수 있어야 하기 때문이다.

| 실행 파일 | 하는 일 |
|---|---|
| **`MoAISearch.exe`** | 트레이 상주 앱 — 탐색기 옆 대화 패널, 지정 폴더 자동 색인, 설정 · 모델 받기 |
| **`simplerag-rs.exe`** | CLI · 워커 — `index` · `search` · `ask` · `chat` · `status` · `extract` 등 |
| `struct_docparse.dll` | 문서 파싱 · 스캔 OCR — 별도 저장소 [Struct-DocParse-OCR-rust](https://github.com/kobongsoo/Struct-DocParse-OCR-rust) |

> 지금 운영판은 **Rust 판**(`rust/`, 0.1.5)이다. 처음 만든 Python 판(`src/simplerag`, `RAGSearchBox/`)은 측정 · 비교용으로 남아 있다.
> 모든 설계 값은 이 PC 에서의 실측에 근거한다. 측정 기록 `results/REPORT.md` 는 로컬 전용이라 저장소에 없다.

---

## 실측 성능 — 현재 운영값 (2026-11-17 정리)

측정 PC: i5-1340P 노트북(Iris Xe iGPU, 16GB) · iGPU 생성 + 리랭킹(이 PC 의 자동 선택 결과).
Rust 판은 같은 질의에 Python 판과 근거 · 답변이 글자까지 같아(2026-10-23 확인) 아래 값을 그대로 쓴다.

### 답변 모델별 — 기본은 Qwen3-1.7B

| 답변 모델 (`generation.model`) | 용도 | 첫 글자(TTFT) 중앙값 | 골든셋 v2 핵심어 적중 |
|---|---|---:|---:|
| **qwen3-1.7b-q4** | **기본 내장** | **약 2.7초** | **80 / 150** |
| midm-2.0-mini-q4 | 한국어 특화(KT Mi:dm 2.0 Mini) | 약 4.5초 | 86 / 150 |
| qwen3.5-4b-q4 · qwen3-4b-q4 | 정밀 — 설정 > 모델에서 받는다 | 1.7B 의 약 3배 | 골든 100문항 정답 59 |
| qwen3-0.6b-q4 | 빠름(측정용, 배포 안 함) | 약 1.5초 | 57 / 150 |

### 평가 코퍼스 506문항 (368문서 · 59,747청크)

| 지표 | **qwen3-1.7b (기본)** | qwen3-0.6b |
|---|---:|---:|
| 정답률 | **399 / 506 (78.9%)** | 377 / 506 (74.5%) |
| 첫 글자 중앙값 / p90 | 2.67초 / 3.51초 | 1.91초 / 2.55초 |
| 3초 안에 첫 글자 | 71.5% | 97.2% |
| 답변 완료 중앙값 | 6.36초 | 3.62초 |
| 근거 표시(검색 + 리랭킹) 중앙값 | 약 0.44초 | 약 0.44초 |

1.7B 의 정답 증가(+22문항)는 McNemar p=0.007 로 유의하다 — 이 프로젝트에서 처음 확정된 품질 상승이다(REPORT §42, 2026-09-15).

### 색인 · OCR · 메모리

| 항목 | 값 | 측정 |
|---|---|---|
| 전체 색인 | PDF 30건 · 925쪽 · 4,616청크 약 61분(4스레드) → 임베딩 8스레드로 **1.30배** | 2026-10-30, 시간의 93% 가 벡터 생성 |
| 스캔 OCR | 쪽당 약 **3~4초**(A4 스캔 2.8~3.9초, 누운 A6 스캔 137쪽 평균 3.9초) — 색인 뒤 대기열에서 차례로 | 2026-11-13 · 11-17 (DLL 0.1.1) |
| 워커 메모리 | 1.7B 를 iGPU 에 올린 채 약 4.8GB(그중 생성 모델 1.7GB) | 2026-11-13 |

---

## 구성

| 항목 | 값 | 근거 |
|---|---|---|
| 추출 | **struct_docparse.dll** — 문서를 제목 · 문단 · 표 · 목록 블록으로 읽음(PDF · DOCX · PPTX · XLSX · HWPX · HWP · DOC · HTML · MD 등) | 분리 전 내장 파서와 결과 같음(실문서 84건) |
| OCR | 스캔 PDF 쪽 · 그림 파일 — PP-OCRv6 검출 + 한국어 PP-OCRv5 인식, 누운 쪽은 바로 세워 읽음(DLL 0.1.1) | 운영 PDF 의 8~10% 쪽이 스캔 |
| 청킹 | structured 128토큰 / 16 겹침 — 절 경계 유지, `[문서명 > 절]` 접두, 표 조각엔 머리글 다시 붙임 | 512 대비 TTFT 3.3배 |
| 임베딩 | e5-small-ko ONNX int8 (384차원) | fp32 대비 2.5배 |
| 검색 | dense 10 + BM25 10 → RRF → 리랭킹(bge-reranker-base int8, iGPU 일 때) → 근거 3건 | top-3 이 실측 포화점 |
| 관련 근거 판정 | 리랭커 점수 + 질문 · 근거 핵심 낱말 겹침 — 관련 근거가 없으면 "답변할 수 없습니다" | 리랭커 점수만으로는 못 가름 |
| 생성 | llama.cpp GGUF, **CPU / iGPU(Vulkan) 자동 선택**(1.3배 이상 빠를 때만 iGPU) | Iris Xe prefill 2.8배 |
| 저장 | 벡터 sqlite(메모리 전수 비교) + BM25 역색인 — 서버 없음 | |

검색 · 청킹 계수, 답변 모델, 시스템 지시문, OCR 옵션은 모두 **`config.yaml`** 에서 코드 수정 없이 바꾼다(우선순위: 환경변수 > config.yaml > 기본값).
`chunk.*` · `extract.*` 를 바꾸면 다시 색인(`index --rebuild`)해야 하고, 나머지는 다음 실행부터 반영된다.

---

## 빌드 (Rust 판 — Windows x64)

### 준비물

| 필요한 것 | 쓰임 |
|---|---|
| [Rust](https://rustup.rs) stable (MSVC) + Visual Studio C++ 빌드 도구 | `simplerag-rs.exe` · `MoAISearch.exe` 빌드 |
| 문서 파서 `struct_docparse.dll` | 아래 1단계 — **Releases 에서 받거나** 소스에서 빌드 |
| Python 배포본 `dist\simplerag` (처음 한 번) | 런타임 DLL(llama.cpp CPU · Vulkan, VC++ 런타임)과 모델(e5 · bge · GGUF)을 여기서 가져온다 — 아래 2단계 |
| Microsoft Edge WebView2 런타임 | `MoAISearch.exe` 패널 화면(Windows 11 은 기본 설치) |
| (선택) [Inno Setup 6](https://jrsoftware.org/isinfo.php) | 설치 파일 만들기 |

```
git clone https://github.com/kobongsoo/SimpleRAG.git
```

### 1단계 — 문서 파서 DLL 준비 (둘 중 하나)

`simplerag-rs.exe` 는 실행할 때 **exe 옆의 `struct_docparse.dll`** 을 불러 쓴다. 이 저장소에는 DLL 이 없다.

**방법 A — GitHub Releases 에서 받기 (빌드 없음, 권장)**

1. [Struct-DocParse-OCR-rust Releases](https://github.com/kobongsoo/Struct-DocParse-OCR-rust/releases) 에서 최신
   `struct_docparse-<버전>-<커밋>-windows-x64.zip` 을 받는다(지금 **0.1.1**).
2. 아무 곳에나 푼다. 안에 `struct_docparse-…-windows-x64\lib\struct_docparse.dll` 이 있다
   (같은 `lib\` 에 pdfium · onnxruntime · OCR 모델도 함께 들어 있다).
3. 2~3단계를 하기 전에 **`SDP_DIST` 에 그 풀린 폴더**를 알려 준다.

```powershell
# 예: gh CLI 로 받기 (브라우저로 받아도 된다)
gh release download v0.1.1 -R kobongsoo/Struct-DocParse-OCR-rust -p "*windows-x64.zip" -D C:\sdp
Expand-Archive C:\sdp\struct_docparse-0.1.1-a3a7091-windows-x64.zip C:\sdp
$env:SDP_DIST = "C:\sdp\struct_docparse-0.1.1-a3a7091-windows-x64"
```

**방법 B — 소스에서 빌드**

SimpleRAG 와 **같은 상위 폴더**에 받아 빌드한다(다른 곳이면 환경변수 `SDP_ROOT` 에 그 경로).

```
D:\Project\
├─ SimpleRAG\
└─ Struct-DocParse-OCR-rust\
```

```powershell
git clone https://github.com/kobongsoo/Struct-DocParse-OCR-rust.git
cd Struct-DocParse-OCR-rust
cargo build --release -p sdp-ffi        # → target\release\struct_docparse.dll
```

`SDP_DIST` 가 있으면 방법 A, 없으면 방법 B 의 위치를 쓴다. 어느 쪽에도 DLL 이 없으면 3단계가 "문서 파서 DLL 이 없습니다" 로 멈춘다.

### 2단계 — 런타임 · 모델 준비 (처음 한 번)

3단계의 `build_dist.ps1` 은 런타임 DLL 과 모델을 **Python 배포본 `dist\simplerag`** 에서 복사 · 하드 링크한다(같은 드라이브면 수 GB 모델이 두 벌 생기지 않는다).
이미 `dist-rust\` 에 있는 파일은 다시 가져오지 않으므로, 한 번 만든 뒤에는 이 단계가 필요 없다.

| 가져오는 것 | 원본 위치 | 없을 때 |
|---|---|---|
| llama.cpp CPU · Vulkan DLL, VC++ 런타임 | `dist\simplerag\_internal\` · `runtime\llama_vulkan\` | — (생성이 안 됨) |
| onnxruntime(1.29) · pdfium | `dist\simplerag\_internal\` | 방법 A 면 Releases 묶음의 `lib\` |
| 임베딩 e5-small-ko · 리랭커 bge int8 · 답변 모델 GGUF(1.7B · 4B) | `dist\simplerag\models\` | — (리랭커가 없으면 리랭킹만 꺼짐) |
| OCR 모델 3개 | `dist\simplerag\models\ocr\` | DLL 저장소 `models\ocr\` 또는 Releases 묶음 `lib\models\ocr\` |

Python 배포본 만들기 — [Python 판](#python-판-측정--비교용) 설치 후:

```powershell
.venv\Scripts\python.exe bench\download_models.py     # 답변 모델 GGUF 받기(Hugging Face)
.venv\Scripts\python.exe build_exe.py                  # → dist\simplerag (자세히: doc/03_exe_배포_가이드.md)
```

### 3단계 — 빌드해서 `dist-rust\` 에 모으기

```powershell
cd SimpleRAG
powershell -ExecutionPolicy Bypass -File rust\build_dist.ps1
```

`cargo build --release` 뒤 `dist-rust\` 에 `simplerag-rs.exe` · `MoAISearch.exe` · `struct_docparse.dll` · 런타임 · 모델 · 사용 설명서 · 라이선스 문서(`.venv` 가 있을 때)를 모은다.

- 설정(`config.yaml` · `RAGSearchBox.ini`)과 색인은 **이미 있으면 덮어쓰지 않는다**.
- MoAISearch 가 켜져 있으면 잠긴 exe · DLL 만 건너뛰고 알려 준다 — 트레이에서 끄고 다시 실행한다.
- **DLL 만 바꿀 때**: 1단계(새 Releases 묶음 또는 다시 빌드) 뒤 이 3단계만 다시 한다. DLL 의 `blocks_version` 이 같으면 색인은 그대로 쓴다.

### 4단계 — (선택) 설치 파일

```powershell
powershell -ExecutionPolicy Bypass -File rust\build_installer.ps1
```

→ `dist-rust\MoAISearch_Setup_<버전>.exe` (버전은 `rust\ragsearchbox\Cargo.toml`). DLL 도 함께 설치된다.
자세한 절차는 [doc/MoAISearch-설치파일-빌드-설명서-20261102.html](doc/MoAISearch-설치파일-빌드-설명서-20261102.html).

---

## 실행

### 앱 — `MoAISearch.exe`

```powershell
dist-rust\MoAISearch.exe
```

1. 트레이에 상주한다. 처음 실행하면 환영 화면과 사용 안내가 뜬다.
2. **설정 > 폴더**에서 검색할 폴더를 추가하면 자동으로 색인한다(폴더 변경도 감시해 반영). 스캔 문서는 색인 뒤 쉬는 동안 OCR 한다.
3. 탐색기에서 그 폴더를 열면 오른쪽에 **대화 패널**이 붙는다 — 질문하면 근거 3건이 먼저, 답이 뒤따라 나온다. 검색 범위는 그 폴더(하위 포함)다.
4. 답변 모델은 **설정 > 모델**에서 받고 고른다.

사용법 전체: [doc/MoAISearch-사용자-설명서-20261102.html](doc/MoAISearch-사용자-설명서-20261102.html) (설치본에서는 패널의 [?] → 사용 설명서).
로그: `%LOCALAPPDATA%\MoAISearch\log\`, 색인 실패 문서는 설정 > 로그.

### CLI — `simplerag-rs.exe`

```powershell
dist-rust\simplerag-rs.exe index --dir "D:\문서함"      # 색인(증분 — 추가 · 수정 · 삭제 반영)
dist-rust\simplerag-rs.exe chat                         # 대화형 — 모델을 한 번만 올리고 계속 질문
dist-rust\simplerag-rs.exe ask "출장비 정산 기한은?"     # 한 번만 묻기(모델 적재 약 6초 포함)
```

| 명령 | 설명 |
|---|---|
| `index --dir <폴더>` | 재귀 · 증분 색인. 한꺼번에 많이 사라지면(30% 또는 50건 이상) 멈춘다 — 정말 지운 것이면 `--allow-delete`. `--rebuild` 전체 재구축 |
| **`chat`** | 모델을 한 번 올리고 계속 질문 — **여러 개 물을 때는 이것**. MoAISearch 의 워커로도 쓰인다 |
| `ask <질문>` | 한 번만 묻기. `--model <별칭>` 그 질문만 다른 모델, `--folder <폴더>` 그 폴더 문서만 근거로 |
| `search <질문>` | 검색만(모델 안 올림) — 원인 진단용 |
| `extract <파일>` | 추출 미리보기. `--blocks` 블록 · 절 · 청크까지, `--dump <폴더>` 파일로 |
| `status [-v]` | 색인 규모 · 문서별 청크 수 · 지금 설정 값 |
| `clear` | 색인 삭제(`-y` 묻지 않음, `--doc <파일>` 한 건만) |
| `backend [--reprobe]` | 생성 백엔드(CPU / iGPU) 확인 · 다시 측정 |
| `import --from dist\simplerag` | Python 판 색인 가져오기(다시 임베딩 없음) |

---

## Python 판 (측정 · 비교용)

처음 만든 판이다. 벤치 하네스(`bench/`)와 2단계의 Python 배포본(`dist\simplerag`)을 만드는 데 쓴다.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe bench\download_models.py
.venv\Scripts\python.exe src\simplerag\cli.py index --dir "D:\문서함"
.venv\Scripts\python.exe src\simplerag\cli.py chat
```

- 형제 프로젝트 MpowerClassify(텍스트 추출 모듈 · e5 임베딩 모델)가 같은 PC 에 있어야 한다 —
  `D:\Project\MpowerClassify` 를 찾고, 다르면 `SIMPLERAG_CSO_SRC` · `SIMPLERAG_EMBED_DIR` 로 지정.
- iGPU 를 쓰려면 `runtime\llama_vulkan\` 에 같은 버전의 Vulkan 판 llama.cpp DLL 이 있어야 한다(`SOURCE.txt`).
- 회귀 측정: `.venv\Scripts\python.exe bench\eval_large.py --stage retrieval` (설정을 바꾼 뒤 반드시).

---

## 알려진 한계

| 항목 | 내용 |
|---|---|
| 생성 정답률 약 79% | 약 21% 는 틀린다 — 근거를 항상 먼저 보여 주는 이유 |
| 첫 글자 3초 | 1.7B 는 3초 안이 약 72% — 빠른 응답이 우선이면 더 작은 모델, 정확도가 우선이면 4B |
| 표 열 혼동 | 셀 병합 · 불규칙 표에서 열을 헷갈린 답이 나온다(근거 표에는 값이 있어 판정에도 잘 안 잡힘) |
| 짧은 질문 | "며칠" 같은 짧고 일반적인 질문은 많은 청크 속에서 변별력이 낮다 |
| OCR 범위 | 스캔 PDF 쪽 · 그림 파일만. docx · pptx 안의 그림, 쪽 안 일부만 누운 표는 읽지 않는다. 쪽당 3~4초 |
| iGPU 답변 차이 | iGPU 로 생성하면 CPU 와 문장이 달라질 수 있다(정답률 차이는 비유의) |

---

## 문서

| 문서 | 내용 |
|---|---|
| [rust/README.md](rust/README.md) | Rust 판 구조 · Python 판과의 호환 · 워커 프로토콜 |
| [doc/MoAISearch-사용자-설명서-20261102.html](doc/MoAISearch-사용자-설명서-20261102.html) | 앱 사용 설명서 |
| [doc/MoAISearch-설치파일-빌드-설명서-20261102.html](doc/MoAISearch-설치파일-빌드-설명서-20261102.html) · [doc/MoAISearch-답변모델-배포-설명서-20261113.html](doc/MoAISearch-답변모델-배포-설명서-20261113.html) | 설치 파일 · 답변 모델 설치 파일 만들기 |
| [doc/04_구현_명세.md](doc/04_구현_명세.md) · [doc/02_테스트_가이드.md](doc/02_테스트_가이드.md) · [doc/03_exe_배포_가이드.md](doc/03_exe_배포_가이드.md) | 구현 명세 · 내 문서로 시험하기 · Python exe 빌드와 `config.yaml` 전체 표 |
| [plan/](plan/) | 설계서 — 구조화 추출, OCR, 문서 파서 DLL 분리, 색인 속도, 메모리 절감 등 |
| [Struct-DocParse-OCR-rust](https://github.com/kobongsoo/Struct-DocParse-OCR-rust) | 문서 파서 DLL — 옵션 · 블록 형식 · OCR |

---

## 라이선스

이 저장소의 코드(Rust `rust/` · Python `src/` · `RAGSearchBox/` · 스크립트 · 설명서)는 [MIT 라이선스](LICENSE)를 따른다.
저장소에 없는 것들은 각자의 라이선스를 따른다.

| 것 | 라이선스 · 위치 |
|---|---|
| 문서 파서 `struct_docparse.dll` | MIT — [Struct-DocParse-OCR-rust](https://github.com/kobongsoo/Struct-DocParse-OCR-rust) (pdfium · ONNX Runtime · PP-OCR 모델 포함, 그 저장소의 `third_party/README.md`) |
| 임베딩 · 리랭커 · 답변 모델(GGUF) · OCR 모델 | 모델마다 다르다 — 배포본의 `LICENSES_MODELS.txt`. 일부 답변 모델(EXAONE · Kanana Nano 등)은 **비상업** 조건 |
| 런타임(onnxruntime · pdfium · llama.cpp) · Rust · Python 라이브러리 | 배포본의 `LICENSES_THIRD_PARTY.txt` (`rust/gen_licenses.py` 가 빌드 때 만든다) |
| `rust/patches/` | llama.cpp(MIT) 에 대는 패치 |
