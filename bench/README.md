# SimpleRAG 속도 실측 벤치

로컬 CPU 전용 RAG가 **실용 속도로 성립하는지** 판정하기 위한 최소 벤치마크.

## 대상 환경

- CPU: Intel i5-1340P (P-core 4 + E-core 8 = 12C/16T), GPU 없음
- RAM: 15.7GB
- 런타임: `llama-cpp-python` (GGUF, CPU 프리빌트 휠)

## 구성

| 파일 | 역할 |
|---|---|
| `probe_models.py` | HF 저장소의 GGUF 파일 목록/크기 조회 (다운로드 전 확인) |
| `download_models.py` | Q4_K_M 가중치를 `../models/` 로 내려받기 |
| `bench_llm.py` | 본 벤치 — prefill / TTFT / decode 측정 |

### 품질 평가 하네스 (나중에 추가된 것들)

속도 벤치로 시작했지만, 실제로 판정이 필요했던 것은 **품질**이었다.

| 파일 | 역할 |
|---|---|
| `build_textcache.py` | 색인된 문서 전체를 **인덱싱과 같은 추출기로** 텍스트 캐시화 |
| `select_passages.py` | 중복군 대표 선정 → 폴더 비례 배분 → 사실밀도 순 지문 추출 |
| `eval_cases_v2.py` | 평가 문항 **204개** / 22카테고리 |
| `validate_cases.py` | 문항 자기검증 — 정답이 원문에 실제로 있는가 |
| `eval_large.py` | 본 평가 (`--stage retrieval\|answer`, `--cases`, `--model`) |
| `compare_runs.py` | 두 실행 **McNemar 짝지은 검정** |
| `test_output_cleanup.py` | 출력 정리 회귀 시험 17건 (인용 번호 · 스트리밍) |
| `sweep_chunk.py` | 청크 크기 전수 비교 |

```
python bench/build_textcache.py                       # 1회
python bench/validate_cases.py --cases eval_cases_v2  # 문항 검증
python bench/eval_large.py --cases eval_cases_v2 --stage retrieval
python bench/eval_large.py --cases eval_cases_v2 --stage answer --model qwen3-0.6b-q4
python bench/compare_runs.py results/A.json results/B.json --key ok
```

> ⚠️ `eval_large.py` 는 결과를 **항상 `results/` 에** 쓴다.
> `SIMPLERAG_HOME` 으로 인덱스를 갈라도 출력은 갈라지지 않는다.
> 실험 전에 기준선 결과 파일을 다른 이름으로 복사해 둘 것.

## 실행

```
.venv\Scripts\python.exe bench\download_models.py
.venv\Scripts\python.exe bench\bench_llm.py --stage threads
.venv\Scripts\python.exe bench\bench_llm.py --stage full --threads <최적값>
```

결과는 `../results/bench_{stage}.json` 에 저장된다.

## 측정 지표

| 지표 | 의미 | RAG에서의 중요도 |
|---|---|---|
| `prefill_tps` | 프롬프트(근거 청크)를 읽는 속도 | **높음** — 컨텍스트 길이에 정비례해 TTFT를 지배 |
| `ttft_s` | 질문 입력부터 첫 글자까지 | **가장 높음** — 체감 속도 그 자체 |
| `decode_tps` | 답변 생성 속도 | 중간 — 스트리밍이면 읽는 속도만 넘으면 됨 |
| `total_s` | 전체 소요 | 참고용 |

## 측정 설계에서 주의한 점

1. **prefill을 항상 차갑게 잰다** — 매 측정 전 `llm.reset()`. 안 하면 llama.cpp의
   프리픽스 재사용 때문에 2회차부터 prefill이 사라져 비현실적으로 빠르게 나온다.
2. **Qwen3 thinking 모드 강제 비활성** — 채팅 템플릿을 직접 만들어 `<think></think>`를
   미리 닫는다. 켜두면 사고토큰 수백 개를 더 생성해 3배 느려진다.
3. **중앙값 사용** — 노트북 CPU는 터보 부스트/발열 스로틀링으로 편차가 커서
   1회 측정은 신뢰할 수 없다. warmup 1회 후 3회 측정의 중앙값.
4. **스레드 스윕** — P/E 하이브리드 CPU는 스레드를 늘린다고 빨라지지 않는다.
   동기화 배리어가 가장 느린 E-core를 기다리기 때문. 실측으로 고른다.
5. **한국어 토큰 수를 실제 토크나이저로 맞춘다** — 문자 길이로 어림하면
   한국어는 오차가 커서 prefill 비교가 흐려진다.
