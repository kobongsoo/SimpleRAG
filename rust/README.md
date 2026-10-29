# SimpleRAG · RAGSearchBox — Rust 판

Python 판(`src/simplerag`, `RAGSearchBox`)과 **같은 기능·같은 입출력**을 내도록 Rust 로 다시 짠 것입니다.
원본 Python 코드는 건드리지 않았습니다.

| 실행 파일 | 하는 일 | Python 판 |
|---|---|---|
| `simplerag-rs.exe` | index · search · ask · chat(워커) · status · clear · extract · backend · warmup · **import** | `simplerag.exe` |
| `RAGSearchBox-rs.exe` | 트레이 상주, 탐색기 옆 대화 패널(WebView2), 자동 인덱싱, 설정 창 | `RAGSearchBox.exe` |

## 1. 빌드·배포

```powershell
powershell -ExecutionPolicy Bypass -File rust\build_dist.ps1
```

- `cargo build --release` 뒤 저장소 루트 `dist-rust\` 에 exe 두 개를 넣습니다.
- 처음이면 Python 배포본(`dist\simplerag`)에서 런타임 DLL(onnxruntime 1.29 · pdfium · llama.cpp CPU/Vulkan)을 복사하고,
  모델(GGUF·e5·bge 리랭커)은 **하드 링크**로 겁니다(같은 드라이브라 수 GB 가 두 벌 생기지 않습니다).
- 이미 있는 설정·인덱스는 덮어쓰지 않습니다.

인덱스가 없으면 Python 판 인덱스를 그대로 가져옵니다(다시 임베딩하지 않음, 4,616청크 0.2초):

```bat
dist-rust\simplerag-rs.exe import --from dist\simplerag
```

## 2. 폴더 구성

```
rust\
├─ Cargo.toml            워크스페이스
├─ build_dist.ps1        빌드 + dist-rust 배포
├─ simplerag\src\        simplerag-rs (라이브러리 + CLI)
│   ├─ config.rs         config.yaml·환경변수 (Python config.py · settings.py)
│   ├─ legacy.rs         기존 추출기 (csoclassify HybridExtractor: PDF·HWP5·DOC·PPT·XLS·DOCX·XLSX·PPTX·HWPX·HTML·TXT)
│   ├─ extract.rs        표 복원·정제·ipynb (extract.py)
│   ├─ docmodel\         구조화 추출(blocks 엔진): pdf · docx · pptx · xlsx · hwpx · doc_bin · html_md · 후처리 · 절 조립
│   ├─ chunk.rs structure.rs   청킹(fixed · structured · blocks)
│   ├─ embed.rs          e5 임베더 · bge 리랭커 (onnxruntime.dll 동적 적재)
│   ├─ store.rs bm25.rs  벡터 저장(sqlite, 메모리 전수 비교) · BM25 역색인
│   ├─ indexer.rs commands.rs  증분 인덱싱(이동·복사 포함) · 워커 인덱싱 명령(@index)
│   ├─ retrieve.rs       하이브리드(RRF) · 리랭킹 · 관련도 판정 · Parent-Child
│   ├─ find.rs           탐색기 모드 찾기 — 문서 ▸ 쪽 ▸ 발췌 (/find · search --group)
│   ├─ generate\         llama.dll FFI · 생성기 · 백엔드(CPU/iGPU) 측정 · 프롬프트 · 표 평문화
│   ├─ app.rs cli.rs main.rs   병렬 예열 · 2단계 응답 · 명령들
│   ├─ pdfium.rs pickle.rs xmldom.rs htmlparse.rs pyutil.rs   보조
└─ ragsearchbox\src\     RAGSearchBox-rs
    ├─ main.rs           이벤트 루프(tao) · 앱 조립 (app.py)
    ├─ worker.rs         워커 수명 관리 + 출력 해석 (rag_worker.py · chat_parser.py)
    ├─ monitor.rs explorer.rs   탐색기 폴더 추적(Shell COM) · 탐색기에서 보기(UIA)
    ├─ panel.rs win.rs   패널 도킹 · Win32 도우미 (chat_panel.py · dock.py)
    ├─ jobs.rs watcher.rs     자동 인덱싱 · 폴더 감시 (index_jobs.py · watcher.py)
    ├─ browse_win.rs     MoAI 탐색기 창(탐색기 모드) · PDF 미리 보기 WebView · file:// 주소
    ├─ folder_icon.rs    인덱싱 폴더(desktop.ini) · 드라이브(HKCU DriveIcons) 아이콘 붙이기 · 되돌리기
    ├─ tray.rs settings.rs history.rs log.rs scope.rs
    └─ ui\panel.html ui\info.html ui\browse.html   패널(RAG) · 설정 창 · 탐색기 모드 화면
```

## 3. Python 판과의 호환

- **워커 프로토콜이 같습니다** — `질문> ` 프롬프트, `── 근거 N건 (ms)` / `── 답변` / `── 소요` 블록,
  `@ev {json}` 전체 근거 줄(`SIMPLERAG_EVIDENCE_FULL=1`), `@index {json}` · `@index-summary {json}` 결과 줄,
  `/ask {"q","folder"}` · `/find {"q","folder"}`(Rust 판만 — `@find` · `@find_done` 줄) · `/index-doc` · `/remove-doc` · `/bm25` · `/index-plan` · `/index-outside`, stderr 의 `리랭커 적재 완료`.
  그래서 Python RAGSearchBox 가 `simplerag-rs.exe` 를 워커로 써도(INI `SimpleRagExe`) 동작하도록 맞췄습니다(이 조합은 직접 시험하지 않았습니다).
  stdin 은 UTF-8 을 먼저 읽고, 아니면 cp949 로 풉니다(Python RAGSearchBox 는 cp949 로 보냄).
- **config.yaml · 환경변수 · index_state.json 모양이 같습니다**(지문·내용 해시·id 범위 규칙 동일).
- **생성 백엔드 측정(gen_backend.json) 키가 같아** Python 판 측정을 그대로 다시 씁니다.
- 벡터·BM25 저장 형식만 다릅니다: `rag_index\points.sqlite`, `bm25_index.bin`. Python 판 인덱스는 `import` 로 옮깁니다.

## 4. 실제 실행으로 확인한 것 (2026-10-23)

| 항목 | 결과 |
|---|---|
| `extract --blocks` 출력(블록·절·청크 전부) | `sample_pdf` PDF 30건 + `D:\분류함` 표본 70건(doc·docx·pptx·ppt·hwp·hwpx·xlsx·xls·html·md·ipynb·py·txt) **모두 글자까지 같음** |
| 인덱싱 `sample_pdf` | 문서 30건 · 청크 4,616개 — 청크 본문 4,616개 모두 같음, 임베딩 코사인 ≥ 0.9999998 |
| 검색(리랭킹 포함) | 질의 12개 상위 결과 모두 같음(Rust 로 만든 인덱스·가져온 인덱스 둘 다) |
| 답변(iGPU, 1.7B · 0.6B) | 질의 3개 답 글자까지 같음 |
| chat 워커 | `/help` `/status` `/ask` `@ev` `/index-plan` `/index-doc`(추가·변경 없음·이동) `/remove-doc`(있는 파일 거절) `/bm25` `/index-outside` |
| RAGSearchBox-rs | 탐색기 감지 → 패널 도킹(탐색기 왼쪽으로 물림) → 워커 기동 → 지정 폴더 밖 확인 → 대조 → 자동 인덱싱 3건 → BM25 → 패널 질문 → 근거·답·소요 줄 표시 → 대화 기록 저장 → 설정 창 → 트레이 '종료'(워커 정상 종료, 탐색기 자리 복원) |

## 5. Python 판과 다른 점

- **옛 방식(탐색기 검색창에 `?질문` + 답변 창, `[Trigger] SearchBox = 1`)은 옮기지 않았습니다.** 기본값이 꺼짐이라
  폴더 패널만 동작합니다(켜 두면 로그에 경고만 남깁니다).
- 근거 전체 본문 `[...]` 는 별도 창 대신 **패널 안 팝업**으로 보입니다.
- 폴더 한정 검색은 항상 메모리의 벡터로 합니다(Python 판의 '300MB 넘으면 Qdrant 필터' 느린 길이 없습니다).
- 패널 너비 INI 값은 **100% 배율 기준**입니다(고배율 모니터에서는 배율을 곱한 픽셀로 놓습니다).
- 다른 이름을 씁니다 — 중복 실행 뮤텍스 `Local\RAGSearchBox-rs`, 자동 시작 값 `RAGSearchBox-rs`,
  로그·대화 기록 `%LOCALAPPDATA%\RAGSearchBox-rs\`. Python 판과 동시에 켜면 패널이 두 개 뜰 수 있습니다.
- 사이냅(snf_exe) 폴백은 `SIMPLERAG_SNF_EXE` 또는 exe 옆 `synap\snf_exe.exe` 가 있을 때만 씁니다(Python 배포본도 동봉하지 않음).
