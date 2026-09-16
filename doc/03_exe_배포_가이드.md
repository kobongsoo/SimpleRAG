# SimpleRAG exe 배포 가이드

Python 설치 없이 실행할 수 있는 실행파일로 묶는 방법과, 다른 PC에 배포하는 절차.

> 테스트 방법은 [02_테스트_가이드.md](02_테스트_가이드.md) 참조.
> exe 로 실행할 때도 명령·옵션은 소스 실행과 완전히 같다
> (`.venv\Scripts\python.exe src\simplerag\cli.py` → `simplerag.exe`).

---

## 1. 빌드

개발 PC(모델과 CSOClassify 가 있는 PC)에서 실행한다.

```
.venv\Scripts\python.exe -m pip install pyinstaller
```

```
.venv\Scripts\python.exe build_exe.py
```

약 1분이면 끝난다(모델 복사 제외).

| 옵션 | 설명 |
|---|---|
| (없음) | 빌드 + 모델 복사 |
| `--no-models` | 빌드만. 모델은 나중에 복사 |
| `--models-only` | 빌드 없이 모델만 복사 |

⚠️ 빌드는 `--clean` 으로 `dist/` 를 비우므로, 재빌드하면 **모델을 다시 복사해야 한다**
(`--models-only`). iGPU 런타임(`runtime\`)은 빌드 때마다 자동으로 다시 복사된다.

### 빌드 결과

```
dist/simplerag/
├─ simplerag.exe          ← 실행 파일
├─ config.yaml            ← 검색·청킹 계수 (§4)
├─ _internal/             ← 파이썬 런타임 · CPU 판 llama.cpp DLL (154MB)
├─ runtime/
│  └─ llama_vulkan/       ← iGPU(Vulkan) 판 llama.cpp DLL (59MB)
└─ models/
   ├─ Qwen3-0.6B-Q4_K_M.gguf     378MB
   ├─ Qwen3-1.7B-Q4_K_M.gguf    1056MB  (정밀 모드용, 선택)
   ├─ e5-small-ko/               129MB
   └─ bge-reranker-base-int8/    283MB  (리랭커)
```

| 구성 | 크기 |
|---|---:|
| 프로그램(exe + _internal) | **154MB** |
| iGPU 런타임(runtime/llama_vulkan) | 59MB |
| 모델 (0.6B + 임베딩 + 리랭커) | 790MB |
| 모델 (1.7B 포함) | 1,846MB |
| **합계** (인덱스 제외) | **약 1.0GB ~ 2.1GB** |

---

## 2. 빌드 설계 — 왜 이렇게 했는가

### onedir (폴더 배포), onefile 아님

`onefile` 은 실행할 때마다 수백 MB 의 DLL(onnxruntime, llama.cpp)을 임시 폴더에
풀어야 해서 기동이 5~10초 더 늘어난다. 이미 모델 적재에 4.7초가 드는데 거기
얹히면 체감이 크게 나빠진다. **`onedir` 은 그 비용이 없다.**

단점은 단일 파일이 아니라는 것뿐이고, 어차피 모델 폴더가 옆에 있어야 하므로
단일 파일의 이점이 애초에 작다.

### 모델은 exe 에 넣지 않는다

GGUF 378MB + ONNX 118MB + 토크나이저 17MB ≈ 0.5GB.
exe 에 넣으면 빌드·배포·갱신이 모두 무거워지고, **모델만 바꿔 끼울 수도 없다.**
빌드 스크립트가 `dist/simplerag/models/` 로 복사해 둔다.

### CSOClassify 는 번들한다

텍스트 추출 모듈(`csoclassify.extract`)은 exe 안에 포함한다.
**대상 PC 에 CSOClassify 가 없어도 문서 추출이 동작한다.**
빌드 시점에만 소스가 필요하다(`SIMPLERAG_CSO_SRC`, 기본 `D:\Project\CSOClassify\src`).

> 사이냅 필터(`snf_exe.exe`)는 번들하지 않는다. 하이브리드 추출기는 포맷별
> 전용 파서를 먼저 쓰고 사이냅은 폴백일 뿐이라, 없어도 실측 18/18 성공했다.

### llama.cpp DLL 을 두 벌 넣는다 (CPU 판 + iGPU 판)

답변 생성은 PC 에 따라 CPU 또는 내장 GPU(iGPU)로 한다(REPORT §31·§32).
이 노트북(Iris Xe)에서 iGPU 는 prefill 이 2.8배 빨라 TTFT 가 2.42→1.32초가 됐다.

| 판 | 위치 | 쓰는 경우 |
|---|---|---|
| CPU 판 | `_internal\llama_cpp\lib\` (PyInstaller 가 담는다) | iGPU 가 없거나 느리거나 깨졌을 때 |
| Vulkan 판 | `runtime\llama_vulkan\` (빌드 스크립트가 복사) | iGPU 가 CPU 보다 1.3배 이상 빠를 때 |

**한 벌로 합칠 수 없다.** Vulkan 판은 `vulkan-1.dll` 을 직접 링크해서 그래픽
드라이버가 없는 PC 에서는 적재 자체가 안 된다. 또 `n_gpu_layers=0` 으로 두어도
큰 행렬곱은 iGPU 로 보내므로 'CPU 모드'가 되지 않는다.

앱은 **첫 실행 때 두 판을 자식 프로세스로 한 번씩 올려 속도를 잰 뒤** 빠른 쪽을
고르고 데이터 루트의 `gen_backend.json` 에 저장한다(약 15~40초, 1회). 자식이 죽어도
앱은 멀쩡하고 CPU 로 간다. 런타임 폴더는 데이터 루트가 아니라 **exe 폴더 기준**으로
찾는다 — `SIMPLERAG_HOME` 으로 인덱스를 다른 곳에 두어도 iGPU 가 꺼지지 않는다.

⚠️ `runtime\llama_vulkan\VERSION.txt` 의 버전이 파이썬 `llama-cpp-python` 과 같아야
한다(지금 0.3.35). 다르면 앱이 iGPU 판을 쓰지 않는다. 패키지를 올리면 같은 버전의
Vulkan 휠(`llama_cpp/lib`)로 이 폴더도 바꿀 것(출처는 `SOURCE.txt`).

---

## 3. 다른 PC 에 배포

`dist/simplerag/` 폴더를 통째로 복사하면 끝이다. **Python 설치 불필요.**

```
D:\SimpleRAG\
├─ simplerag.exe
├─ _internal\
├─ runtime\
└─ models\
```

### 실행

```
D:\SimpleRAG\simplerag.exe index --dir "D:\문서"
```

```
D:\SimpleRAG\simplerag.exe chat
```

주요 명령은 소스 실행과 동일하다.

| 명령 | 설명 |
|---|---|
| `index --dir <폴더>` | 재귀 인덱싱(증분). `--rebuild` 로 전체 재구축 |
| `chat` | 대화형 질의 (모델 1회 적재) |
| `ask <질문>` | 1회용 질의 |
| `search <질문>` | 검색만 (LLM 미적재) |
| `extract <파일>` | 추출 미리보기 |
| **`clear`** | **인덱스 삭제** (`-y` 무인, `--doc <파일>` 1건만) |
| `status [-v]` | 인덱스 상태 |
| `warmup` | 예열만 수행 |
| **`backend`** | **생성 백엔드(CPU/iGPU) 확인. `--reprobe` 로 다시 측정** |

### 요구 사항

| 항목 | 값 |
|---|---|
| OS | Windows 10/11 x64 |
| RAM | 8GB 이상 (0.6B 기준). 1.7B 는 12GB 권장 |
| 디스크 | 프로그램·모델 1.7GB + **인덱스**(문서 4만 청크 기준 약 200MB) |
| GPU | 불필요. 내장 GPU(Intel Iris Xe·Arc, AMD Radeon 등)가 있고 드라이버가 Vulkan 을 지원하면 재서 빠를 때만 쓴다 |
| 인터넷 | 불필요 (완전 오프라인) |

---

## 4. 데이터 위치

**exe 가 있는 폴더가 기본 데이터 루트**다. 인덱스·상태 파일이 여기에 생긴다.

```
D:\SimpleRAG\
├─ simplerag.exe
├─ qdrant_data\        ← 벡터 인덱스 (자동 생성)
├─ bm25_index.npz      ← BM25 캐시
├─ index_state.json    ← 증분 갱신 상태
└─ gen_backend.json    ← CPU/iGPU 측정 결과 (첫 질문 때 자동 생성)
```

### 데이터를 다른 곳에 두려면

`Program Files` 처럼 쓰기 권한이 없는 곳에 exe 를 두는 경우 필요하다.

```
set SIMPLERAG_HOME=D:\내인덱스
D:\SimpleRAG\simplerag.exe status
```

| 환경변수 | 용도 | 기본값 |
|---|---|---|
| `SIMPLERAG_HOME` | 데이터 루트(인덱스·모델) | exe 폴더 |
| `SIMPLERAG_EMBED_DIR` | 임베딩 모델 폴더 | `<HOME>\models\e5-small-ko` |
| `SIMPLERAG_CSO_SRC` | csoclassify 추출 모듈 소스(소스 실행·빌드 시) | `D:\Project\MpowerClassify\src`, 없으면 `D:\Project\CSOClassify\src` |
| `SIMPLERAG_MODELS_DIR` | GGUF 폴더(데이터 루트와 분리할 때) | `<HOME>\models` |
| `SIMPLERAG_CHUNK_TOKENS` | 청크 크기 | `128` |
| `SIMPLERAG_CHUNK_OVERLAP` | 청크 겹침 | 청크의 12.5% |
| `SIMPLERAG_GEN_BACKEND` | 생성 백엔드 `auto` / `cpu` / `vulkan` | `auto` (재서 고름) |
| `SIMPLERAG_RERANK` | 리랭킹 `auto` / `1` / `0` | `auto` (iGPU 일 때만 켬) |
| `SIMPLERAG_VULKAN_LIB` | Vulkan 판 DLL 폴더 | `<exe 폴더>\runtime\llama_vulkan` |
| `SIMPLERAG_RERANK_DIR` | 리랭커 모델 폴더 | `<MODELS_DIR>\bge-reranker-base-int8` |
| `SIMPLERAG_RERANK_SHARE_TOKENIZER` | `0` 이면 리랭커 토크나이저를 따로 파싱(기동 약 0.6초 느려짐) | `1` |
| `SIMPLERAG_RERANK_DEFER` | `0` 이면 리랭커를 준비 완료 전에 함께 올림(chat 준비 약 0.6초 느려짐) | `1` |
| `SIMPLERAG_CONFIG` | 설정 파일 경로(`-` 이면 config.yaml 무시) | 데이터 폴더 → exe 폴더의 config.yaml |

> `SIMPLERAG_CHUNK_TOKENS` 를 바꾸면 **전체 재인덱싱(`index --rebuild`)이 필요하다.**
> 증분 갱신은 옛 크기의 청크를 그대로 둔다.
> 128/192/256 실측 비교는 [REPORT §17](../results/REPORT.md) 참조 — **기본값 128 을
> 권장한다.** 256 은 정답률이 같은 수준이면서 TTFT 가 2배가 된다.

### 검색·청킹 계수 바꾸기 — `config.yaml`

코드를 고치지 않고 청크 크기·검색 후보 수·리랭커 입력 수 등을 바꿀 수 있다(REPORT §35).
exe 옆(빌드가 복사해 둔다)이나 데이터 폴더에 `config.yaml` 을 둔다.

| 찾는 순서 | 위치 |
|---|---|
| 1 | 환경변수 `SIMPLERAG_CONFIG` 로 지정한 파일 (`-` 이면 파일 없이 기본값) |
| 2 | `<데이터 폴더>\config.yaml` (`SIMPLERAG_HOME`) |
| 3 | `<exe 폴더>\config.yaml` |

```yaml
chunk:
  tokens: 128                   # 청크 크기(토큰)
  overlap: 16                   # 긴 절을 쪼갤 때만 겹치는 토큰 수
  mode: structured              # structured | fixed
  min_tokens: 16                # 이보다 짧은 꼬리 조각은 버림
  table_header_max_tokens: 64   # 표 조각에 다시 붙이는 머리글 최대 길이
retrieval:
  dense_top_k: 10               # 코사인 검색 후보 수
  bm25_top_k: 10                # BM25 검색 후보 수 (0 = BM25 끔)
  rrf_k: 60                     # RRF 점수 = Σ 1/(rrf_k + 순위)
  bm25_k1: 1.5
  bm25_b: 0.75
  dedup: true
rerank:
  mode: auto                    # auto | on | off
  pool: 10                      # 리랭커 입력 수
generation:
  top_k: 3                      # sLLM 에 넣는 근거 청크 수
```

| 바꾼 항목 | 언제 반영되나 |
|---|---|
| `chunk.*` | **`index --dir <폴더> --rebuild`** 전체 재인덱싱 후. 설정이 인덱스와 다른 채 증분 `index` 를 돌리면 옛 청크와 섞이지 않게 **거부**한다 |
| `retrieval.bm25_k1` / `bm25_b` | 다음 `index` 실행 때(증분이어도 BM25 는 전체를 다시 만든다) |
| 나머지 | 다음 실행부터 바로 |

- 우선순위: **환경변수 > config.yaml > 코드 기본값.** 기존 환경변수(`SIMPLERAG_CHUNK_TOKENS` 등)가 이긴다.
- 오타(`token:`)·범위 밖 값·잘못된 타입은 무시하지 않고 **시작할 때 무엇을 고칠지 알려 주고 멈춘다**
  (`설정 오류: … 알 수 없는 키 'chunk.token' (혹시 'tokens'?)`, 종료코드 2).
- 지금 적용된 값은 `simplerag.exe status` 로 본다(설정 파일 경로·청킹·검색 계수·인덱스 기록 비교).

> **개발 PC 의 기존 인덱스를 그대로 쓰려면**
> `set SIMPLERAG_HOME=D:\Project\SimpleRAG` 로 지정하면 본 인덱스(59,747청크,
> 2026-08-24 기준)를 재인덱싱 없이 바로 쓴다.

> ⚠️ **exe 는 빌드 시점의 코드로 얼어붙는다.** dedup·구조 인식 청킹 같은
> 검색 품질 로직도 코드다 — 인덱스만 최신으로 바꿔 끼워도 **exe 바이너리 자체가
> 옛날 코드면 그 로직이 동작하지 않는다.** 실제로 겪었다(REPORT §25):
> 2026-08-21 10:32 에 빌드된 exe 는 dedup 코드가 생기기 전이라, 같은 문서의
> 거의 동일한 청크 3개가 그대로 top-3 를 채우고 있었다.
> **코드를 고친 뒤에는 데이터만 갱신하지 말고 `build_exe.py` 를 다시 돌려라.**

---

## 5. 설치 후 확인 절차

```
simplerag.exe warmup
```

구성요소가 모두 올라오면 정상이다. 첫 줄에 이 PC 에서 고른 생성 백엔드가 나온다.
**처음 실행하면 CPU/iGPU 속도 측정이 먼저 돈다(약 15~40초, 1회).**

```
[backend] 이 PC 의 CPU/iGPU 생성 속도를 잽니다 (처음 한 번, 약 15~40초)
[backend] iGPU(Vulkan) 선택 — iGPU prefill 700 t/s = CPU 252 t/s 의 2.8배
  생성      iGPU(Vulkan) / 리랭킹 켬
  embedder  2313ms
  qdrant    4623ms
  bm25      2399ms
  llm       1698ms
  llm_warm   264ms      ← 예열 생성(셰이더 컴파일·시스템 프롬프트)
  합계      9174ms (병렬 총합 = 준비 완료)
  reranker  1368ms (준비 완료 뒤 백그라운드)
```

CPU/iGPU 를 고른 이유와 측정값은 `simplerag.exe backend` 로 따로 볼 수 있다.

| 증상 | 원인 | 대처 |
|---|---|---|
| `llm ❌ 생성 모델 없음` | GGUF 미복사 | `models\Qwen3-0.6B-Q4_K_M.gguf` 배치 |
| `embedder ❌ model.onnx 없음` | 임베딩 모델 미복사 | `models\e5-small-ko\` 배치 |
| `추출 모듈을 찾을 수 없습니다` | 번들 실패 | 빌드 PC 에 CSOClassify 가 있었는지 확인 후 재빌드 |
| 쓰기 권한 오류 | exe 가 보호된 경로에 있음 | `SIMPLERAG_HOME` 지정 |
| `생성 CPU` 인데 iGPU 가 있는 PC | 드라이버에 Vulkan 이 없거나 오래됨, 또는 iGPU 가 1.3배 미만으로 빠름 | `backend` 로 이유 확인. 드라이버 갱신 후 `backend --reprobe` |
| iGPU 인데 `리랭킹 끔` | 리랭커 모델 미복사 | `models\bge-reranker-base-int8\` 배치 |
| 첫 실행의 예열만 유난히 오래 걸림 | 속도 측정(1회) + 새 PC 의 셰이더 컴파일(1회) | 정상. 다음 실행부터 사라진다 |

이어서 실제 인덱싱·질의는 [02_테스트_가이드.md](02_테스트_가이드.md) 를 따른다.

---

## 6. 실측 — 신규 설치 시나리오

빈 `dist/simplerag/` 폴더에서 처음부터 수행한 결과.

```
> simplerag.exe index --dir "D:\분류함\2.회사규정"
문서 18건 / 변경 18건 / 청크 394개 / 7.4s

> simplerag.exe ask "본인 결혼 시 경조금은 얼마인가요?"
── 근거 3건 (8ms) ──
  [1] 03.경조사지원규정.doc
      ... | 본인 결혼 | 5 | 500,000 | | 자녀 결혼 | 1 | 300,000 | ...
── 답변 ──
본인 결혼 시 경조금은 500,000 원입니다. [1]
  첫 글자 2.52s / 완료 4.95s
```

생성된 데이터: `qdrant_data` 1.8MB + `bm25_index.npz` 0.3MB + `index_state.json`

**소스 실행과 성능 차이는 없다.** PyInstaller 는 같은 파이썬 코드를 같은
인터프리터로 돌린다.

---

## 7. 빌드 중 발견해 고친 문제

exe 배포를 준비하며 실제로 두 개의 버그가 드러났다. 기록해 둔다.

### 7-1. 🔴 데이터가 임시 폴더에 생성됨

`config.ROOT` 가 `__file__` 기준이었다. PyInstaller 는 코드를 임시 폴더
(`_MEIPASS`)에 풀기 때문에, **인덱스가 임시 폴더에 생겼다가 종료할 때마다
사라질** 상황이었다.

```python
def _data_root():
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))   # exe 옆
    return .../프로젝트 루트                                        # 소스 실행
```

### 7-2. 🔴 리다이렉트 시 UnicodeEncodeError

```
UnicodeEncodeError: 'cp949' codec can't encode character '\u2014'
```

화면 구분선에 쓰는 `—`(U+2014)가 cp949 에 없다. 출력이 파이프나 파일로
리다이렉트되면 Python 이 콘솔 대신 로케일 인코딩(한국어 Windows = cp949)을
쓰기 때문에 죽는다.

콘솔에 직접 쓸 때는 `WriteConsoleW` 를 타서 문제가 없어 **발견이 늦었다.**
소스 실행에도 있던 버그다.

```python
stream.reconfigure(encoding="utf-8", errors="replace")
```

`errors="replace"` 까지 걸어 어떤 문자가 오더라도 출력 때문에 죽지 않게 했다.

> **교훈**: exe 로 묶는 작업은 단순 포장이 아니다. 경로 가정과 인코딩 가정이
> 드러난다. 배포 전에 **리다이렉트 실행**(`> out.txt`)을 반드시 테스트할 것.

---

## 8. 재빌드가 필요한 경우

| 변경 | 재빌드 | 비고 |
|---|:--:|---|
| `src/simplerag/**` 코드 | ✅ | |
| 의존성 추가 | ✅ | `build_exe.py` 의 `COLLECT_ALL`/`HIDDEN` 에도 추가 |
| CSOClassify extract 수정 | ✅ | 번들이므로 |
| 모델 교체 | ❌ | `models\` 에 파일만 바꿔 넣으면 된다 |
| 인덱싱 대상 문서 | ❌ | `index` 로 갱신, `clear` 로 초기화 |
| 설정값(`config.py`) | ✅ | 상수가 코드에 박혀 있다 |
| `config.yaml` 값 | ❌ | 파일만 고친다. `chunk.*` 는 `index --rebuild` 필요 |
| `llama-cpp-python` 버전 변경 | ✅ | `runtime\llama_vulkan\` 도 같은 버전 Vulkan 휠의 DLL 로 교체(`VERSION.txt` 포함) |
| 그래픽 드라이버 갱신 | ❌ | `backend --reprobe` 로 다시 측정 |

---

## 관련 문서

- [README.md](../README.md) — 프로젝트 개요
- [02_테스트_가이드.md](02_테스트_가이드.md) — 단계별 테스트·진단
- [../plan/설계서.md](../plan/설계서.md) — 설계 결정과 근거
