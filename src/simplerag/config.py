#------------------------------------------------------------------
# 전역 설정/기본값 모음
#=> 설계서에서 실측으로 확정한 상수를 한 곳에 모은다. 여기 값만 바꾸면 전체
#   동작 기본값이 바뀐다. 각 값 옆에 '왜 이 값인지'(실측 근거)를 남겨 두어
#   나중에 임의로 바꾸다 성능이 무너지는 일을 막는다.
#------------------------------------------------------------------

import os
import sys
from dataclasses import dataclass


# ── 경로 ─────────────────────────────────────────────
FROZEN = getattr(sys, "frozen", False)


#------------------------------------------------------------------
# 데이터 루트 결정 (핵심 — exe 배포 대응)
#=> 인덱스·모델·상태 파일이 놓일 기준 폴더다.
#
#   소스 실행: src/simplerag/config.py 기준 두 단계 위 = 프로젝트 루트
#   exe  실행: **exe 가 있는 폴더**
#     PyInstaller 는 코드를 임시 폴더(_MEIPASS)에 풀기 때문에, __file__ 을
#     기준으로 잡으면 인덱스가 임시 폴더에 생겼다가 종료 시 사라진다.
#     반드시 sys.executable 기준으로 잡아야 한다.
#
# -out: 절대 경로
#------------------------------------------------------------------
def _data_root():
    override = os.environ.get("SIMPLERAG_HOME")
    if override:
        return os.path.abspath(override)
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


ROOT = _data_root()

# 프로그램 폴더 — 데이터 루트(SIMPLERAG_HOME)와 무관하게 '프로그램이 설치된 곳'.
#   exe 는 exe 폴더, 소스 실행은 프로젝트 루트. 동봉 런타임(DLL)은 데이터가 아니라
#   프로그램의 일부라 여기를 기준으로 찾는다 — SIMPLERAG_HOME 으로 인덱스만 다른
#   곳에 두었는데 iGPU 가 조용히 꺼지는 일이 없게 한다.
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
           else os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


# ── config.yaml (REPORT §35) ─────────────────────────
# 검색·청킹 계수를 코드 수정 없이 바꾸는 파일. 우선순위: 환경변수 > config.yaml > 아래 기본값.
# 찾는 순서: SIMPLERAG_CONFIG → <데이터 루트>/config.yaml → <프로그램 폴더>/config.yaml.
from .settings import ConfigError, check_resolved, load_settings  # noqa: E402,F401

CONFIG_PATH, _YAML = load_settings(ROOT, APP_DIR)
CONFIG_WARNINGS = []


#------------------------------------------------------------------
# 설정값 하나 고르기 — 환경변수 > config.yaml > 기본값
#=> 기존 환경변수(SIMPLERAG_CHUNK_TOKENS 등)는 그대로 가장 먼저 본다 — 실험 스크립트가
#   환경변수로 조건을 고정하기 때문이다. 그다음 config.yaml, 없으면 코드 기본값.
#
# -in: key     = config.yaml 의 "섹션.키" (예: "chunk.tokens")
# -in: default = 코드 기본값
# -in: env     = 환경변수 이름(None 이면 보지 않음)
# -in: cast    = 환경변수 문자열을 바꿀 타입(기본 int)
#
# -out: 최종 값
# -out: error = 환경변수를 cast 로 바꿀 수 없으면 ConfigError
#------------------------------------------------------------------
def _setting(key, default, env=None, cast=int):
    raw = os.environ.get(env) if env else None
    if raw not in (None, ""):
        try:
            return cast(raw)
        except ValueError:
            raise ConfigError("환경변수 {}={!r} 을(를) 읽을 수 없습니다".format(env, raw))
    return _YAML.get(key, default)


# 형제 프로젝트(텍스트 추출 모듈 + 임베딩 모델 원본) 후보 — 앞에서부터 찾는다.
#   2026-09 에 CSOClassify 가 MpowerClassify 로 이름이 바뀌어 소스 실행이
#   통째로 깨진 적이 있다(exe 는 모델을 번들해서 무사했다). 한 경로에
#   못 박지 않고 후보를 나열해 둔다. 안의 패키지 이름은 그대로 csoclassify 다.
_SIBLING_ROOTS = (
    r"D:\Project\MpowerClassify",
    r"D:\Project\CSOClassify",       # 옛 이름 — 다른 PC 에 남아 있을 수 있다
)


#------------------------------------------------------------------
# 형제 프로젝트 하위 경로 찾기
#=> _SIBLING_ROOTS 를 순서대로 돌며 <루트>/<sub> 가 실제로 있는 첫 경로를
#   돌려준다. 아무 데도 없으면 첫 후보 기준 경로를 그대로 돌려준다 —
#   여기서 죽이지 않고 실제 적재 시점의 오류 메시지에 경로가 찍히게 한다.
#
# -in: sub = 루트 아래 상대 경로 (예: "resources/models/e5-small-ko")
#
# -out: 절대 경로 (존재하지 않을 수도 있다)
# -out: error = 없음
#------------------------------------------------------------------
def _sibling_path(sub):
    for root in _SIBLING_ROOTS:
        cand = os.path.join(root, sub)
        if os.path.exists(cand):
            return cand
    return os.path.join(_SIBLING_ROOTS[0], sub)


#------------------------------------------------------------------
# 임베딩 모델 폴더 탐색
#=> 다음 순서로 찾는다. exe 로 배포하면 형제 프로젝트가 없을 수 있으므로,
#   exe 옆에 모델 폴더를 복사해 두는 방식을 1순위로 지원한다.
#    1) 환경변수 SIMPLERAG_EMBED_DIR
#    2) <ROOT>/models/e5-small-ko/      ← exe 배포 시 권장 위치
#    3) 형제 프로젝트(_SIBLING_ROOTS)의 resources/models/e5-small-ko/
#       (개발 PC 기본값)
#
# -out: 절대 경로(존재 여부는 확인하지 않는다 — 적재 시점에 검증)
#------------------------------------------------------------------
def _embed_dir():
    override = os.environ.get("SIMPLERAG_EMBED_DIR")
    if override:
        return os.path.abspath(override)

    local = os.path.join(ROOT, "models", "e5-small-ko")
    if os.path.isfile(os.path.join(local, "model.onnx")):
        return local
    return _sibling_path(os.path.join("resources", "models", "e5-small-ko"))


# 형제 프로젝트의 extract 모듈 경로(설계서 결정11).
#   exe 로 빌드할 때는 csoclassify.extract 를 함께 번들하므로 이 경로가 없어도
#   동작한다 — extract.py 가 import 실패 시 번들된 모듈로 넘어간다.
CSO_SRC = os.environ.get("SIMPLERAG_CSO_SRC", _sibling_path("src"))

MODEL_DIR = _embed_dir()                           # 임베딩 ONNX 폴더
# GGUF 폴더. 실험용으로 데이터 루트를 옮길 때 모델까지 복사하지 않도록 분리 지정을
# 허용한다(SIMPLERAG_HOME 만 바꾸고 모델은 원래 자리를 쓰는 경우).
MODELS_DIR = os.environ.get(
    "SIMPLERAG_MODELS_DIR", os.path.join(ROOT, "models"))
QDRANT_DIR = os.path.join(ROOT, "qdrant_data")     # 벡터 인덱스(local 모드)
STATE_PATH = os.path.join(ROOT, "index_state.json")  # 증분 갱신 상태
BM25_PATH = os.path.join(ROOT, "bm25_index.npz")   # BM25 캐시(numpy 역색인)


#------------------------------------------------------------------
# 임베딩 모델 스펙
#=> e5 계열은 입력 앞에 'passage: ' / 'query: ' 를 붙여야 성능이 나온다.
#   그 규약과 차원/상한을 한 묶음으로 들고 다닌다.
#------------------------------------------------------------------
@dataclass(frozen=True)
class EmbedSpec:
    model_dir: str
    model_file: str
    dim: int
    passage_prefix: str
    query_prefix: str
    max_tokens_model: int


EMBED = EmbedSpec(
    model_dir=MODEL_DIR,
    # int8 양자화본. fp32(model.fp32.onnx) 대비 2.5배 빠름 — AVX-512 없는
    # 이 CPU 에서도 int8 이 유리하다는 것을 실측으로 확인했다(REPORT §8.1).
    model_file="model.onnx",
    dim=384,
    passage_prefix="passage: ",
    query_prefix="query: ",
    max_tokens_model=512,
)

# ONNX 스레드 수. 8로 올려도 이득이 없다 — P-core 4개가 실질 상한(REPORT §8.1).
EMBED_THREADS = 4
# 배치 크기. 1→8 에서 65→77 chunk/s, 그 이상은 오히려 하락.
EMBED_BATCH = 8


# ── 청킹 ─────────────────────────────────────────────
# 128토큰: prefill 이 TTFT 를 지배하므로 컨텍스트를 짧게 유지한다.
#   512→128 로 줄이면 TTFT 9.07s→2.71s (REPORT §2). 실문서 검색 품질
#   손해는 없음을 확인했다(Recall@1 100%, REPORT §13.4).
#   128/192/256 을 실문서 45문항으로 전수 비교했다(REPORT §17).
#     128 → 정답률 76% / TTFT 2.53s   ⭐
#     192 → 정답률 76% / TTFT 3.80s   (셋 중 최악 — 같은 점수에 1.27초 더 든다)
#     256 → 정답률 78% / TTFT 5.38s   (+1문항, McNemar p=1.000 → 유의차 없음)
#   정답률은 크기와 무관하게 평평하고 TTFT 만 컨텍스트 길이에 비례해 늘어난다.
#   → 128 유지가 옳다.
#   다만 청크 크기는 '어떤 질문을 맞히는지'를 바꾼다 — 조문형(복리후생)은
#   55→82% 로 오르고 단문형(기술문서·안전관리)은 내려가 서로 상쇄된다.
#   조문형 규정이 주 용도이고 5초 TTFT 를 감내한다면 256 도 합리적이라,
#   환경변수로 덮어쓸 수 있게 열어 둔다(변경 시 전체 재인덱싱 필요 —
#   증분 갱신은 옛 크기의 청크를 그대로 둔다).
CHUNK_TOKENS = _setting("chunk.tokens", 128, "SIMPLERAG_CHUNK_TOKENS")
# 겹침 기본값은 청크의 12.5% — 경계에서 문장이 잘리는 손실을 막는 최소값.
CHUNK_OVERLAP = _setting("chunk.overlap", max(8, CHUNK_TOKENS // 8), "SIMPLERAG_CHUNK_OVERLAP")
MIN_CHUNK_TOKENS = _setting("chunk.min_tokens", 16)       # 꼬리 조각 버리는 기준
# 표 조각에 다시 붙이는 머리글 줄의 길이 상한(토큰). 머리글 판정이 느슨해 Word 목차 필드
#   코드(2,646토큰)나 설명 문장 행이 머리글로 잡혔고, 그 줄이 조각마다 다시 붙어 128토큰
#   설계의 청크가 2,768토큰까지 커졌다(REPORT §35). 64 는 실측 머리글 분포에서 정했다 —
#   실제 넓은 표 머리글(출장여비 53토큰)은 살리고 64토큰 초과 88줄(460청크)을 걸러낸다.
#   청크 최대 길이 = CHUNK_TOKENS + 이 값.
TABLE_HEADER_MAX_TOKENS = _setting("chunk.table_header_max_tokens", 64)
# 청킹 결과가 달라지는 코드 변경마다 올린다. 인덱스에 기록해 옛 방식 청크와 섞이는 것을 막는다.
#   1 = §22 구조 청킹(머리글 무제한 재부착), 2 = §35 머리글 길이 상한
CHUNKER_VERSION = 2

# 청킹 방식.
#   "structured" — 문서 구조(조문/마크다운/번호절)를 인식해 **절 경계를 지키며**
#                  묶는다. 크기는 그대로 두고 '자르는 위치'만 바꾼다.
#                  실측 근거: 회사규정 조문 123개 중 89개(72%)가 청크 경계에
#                  걸려 있었고, 표·양식 문서가 생성 실패의 최대 집단이었다
#                  (REPORT §20.6). 표 청크에는 머리글 줄을 되살려 넣는다.
#   "fixed"      — 종전 고정 크기. 되돌릴 때 쓴다.
#   ⚠️ 바꾸면 전체 재인덱싱이 필요하다(증분은 옛 방식 청크를 그대로 둔다).
CHUNK_MODE = _setting("chunk.mode", "structured", "SIMPLERAG_CHUNK_MODE", str)


# ── 검색 ─────────────────────────────────────────────
# sLLM 에 넣는 근거 수(리랭킹 후 상위 N). 실측 포화점 — 4 이상은 정답률 정체 + TTFT 만 증가
TOP_K = _setting("generation.top_k", 3)
# RRF 융합에 넣을 1차 후보 수 — 두 검색기를 따로 둔다(config.yaml retrieval.*_top_k)
DENSE_TOP_K = _setting("retrieval.dense_top_k", 10)   # 코사인(dense)
BM25_TOP_K = _setting("retrieval.bm25_top_k", 10)     # BM25 (0 이면 BM25 끔 — dense 단독)

# 근거 중복 제거.
#   코퍼스의 50%(367건 중 183건)가 완전 동일 사본이다. 같은 파일이 여러 경로에
#   색인돼 있으면 top-3 가 사본으로 채워져 **서로 다른 근거가 3건이 아니라
#   1~2건**이 된다. 45문항 실측에서 근거 슬롯의 19%가 이렇게 낭비됐고,
#   생성 실패 9건 중 5건이 여기에 해당했다(REPORT §19).
#   끄려면 SIMPLERAG_DEDUP=0.
DEDUP_EVIDENCE = ((os.environ["SIMPLERAG_DEDUP"] != "0") if os.environ.get("SIMPLERAG_DEDUP")
                  else bool(_YAML.get("retrieval.dedup", True)))
RRF_K = _setting("retrieval.rrf_k", 60)   # RRF 순위 완충 상수(관례값). 점수 = Σ 1/(k + 순위)
# BM25 하이퍼파라미터(Lucene 관례값). 가중치를 인덱스 구축 때 미리 계산하므로, 바꾸면
#   다음 index 실행(증분이어도 BM25 는 전체를 다시 만든다)부터 반영된다.
BM25_K1 = _setting("retrieval.bm25_k1", 1.5)
BM25_B = _setting("retrieval.bm25_b", 0.75)
# BM25 토크나이징. 공백 분리는 한국어 조사 때문에 Recall@1 이 67% 로 떨어진다.
# 서브워드로 바꾸면 92~100% 로 회복된다(REPORT §13.4).
BM25_MODE = "subword"

# 리랭킹 — RRF 후보 RERANK_POOL 건을 크로스인코더로 재정렬해 상위 TOP_K 를 쓴다.
#   정답률 +14~15문항/506(CPU·iGPU 세 번 모두 + 방향이나 비유의, REPORT §29·§31),
#   대신 검색에 약 0.45초가 붙는다. CPU 전용이면 TTFT 3초 달성률이 74→53% 로
#   무너져 목표와 양립하지 않았고, iGPU 에서는 98~99% 를 지켰다.
#   auto — 생성 백엔드가 iGPU 일 때만 켠다 (기본)
#   1/0  — 백엔드와 무관하게 강제로 켬/끔
RERANK = (os.environ.get("SIMPLERAG_RERANK", "").strip().lower()
          or _YAML.get("rerank.mode", "auto"))
RERANK_POOL = _setting("rerank.pool", 10)
# 5 → 10 (REPORT §35.9, 사용자 결정 2026-09-14): 506문항 답변 정답 363→379(+16, p=0.056),
#   TTFT 중앙값 +0.36초(3초 이내 99% 유지). §27 의 "풀 5 최선" 은 204문항·검색 단계 지표였다.
RERANK_MAX_TOKENS = 512     # 질의+근거를 합친 길이 상한(모델 한계)
RERANK_THREADS = EMBED_THREADS
RERANK_DIR = os.environ.get(
    "SIMPLERAG_RERANK_DIR", os.path.join(MODELS_DIR, "bge-reranker-base-int8"))
# 리랭커 토크나이저를 임베더가 파싱해 둔 구성요소로 조립한다(파일 파싱 0.88초 → 약 10ms).
#   파싱은 GIL 을 통째로 쥐어 병렬 예열 중 Qdrant 적재를 그만큼 멈춘다(REPORT §33).
#   검증한 두 파일(바이트 크기로 식별)일 때만 재사용하고, 아니면 종전처럼 파일을 파싱한다.
RERANK_SHARE_TOKENIZER = os.environ.get("SIMPLERAG_RERANK_SHARE_TOKENIZER", "1") != "0"
# 리랭커를 병렬 예열에 넣지 않고 **준비 완료 뒤 백그라운드로** 올린다(REPORT §34).
#   리랭커 ONNX 세션 생성(약 0.6초)이 GIL 을 쥐어 병렬 예열 중 Qdrant 적재를 멈추기 때문이다.
#   chat 은 그만큼 빨리 입력을 받는다. 첫 질문이 적재 중에 오면 기다렸다가 리랭킹하므로 결과는
#   같고, ask(준비 직후 바로 질문)의 총시간은 줄지 않는다 — 옮길 뿐이다. 0 이면 종전(병렬 적재).
RERANK_DEFER_LOAD = os.environ.get("SIMPLERAG_RERANK_DEFER", "1") != "0"

# 환경변수까지 합친 최종값끼리의 모순 검사 — 고칠 수밖에 없는 것은 ConfigError, 나머지는 경고
CONFIG_WARNINGS = check_resolved(CHUNK_TOKENS, CHUNK_OVERLAP, MIN_CHUNK_TOKENS,
                                 TABLE_HEADER_MAX_TOKENS, TOP_K, RERANK_POOL,
                                 DENSE_TOP_K, BM25_TOP_K)


# ── 생성 ─────────────────────────────────────────────
GEN_MODELS = {
    "qwen3-0.6b-q4": "Qwen3-0.6B-Q4_K_M.gguf",   # 기본. TTFT 2.75s / 정답률 83%
    "qwen3-1.7b-q4": "Qwen3-1.7B-Q4_K_M.gguf",   # 정밀 모드. TTFT 8.46s / 92%
}
# 정밀 모드(계획서 D8 ③ / REPORT §44): 기본은 빠른 0.6B, 필요할 때만 1.7B 를 쓴다.
#   1.7B 는 506문항 정답 377 → 399(p=0.007)지만 TTFT 3초 이내가 97% → 72% 로 떨어진다(§42).
#   그래서 "기본값을 바꾸는" 대신 "고를 수 있게" 한다 — config.yaml generation.model,
#   환경변수 SIMPLERAG_GEN_MODEL, CLI `--precise` / `--model`.
FAST_GEN_MODEL = "qwen3-0.6b-q4"        # 빠름(기본)
PRECISE_GEN_MODEL = "qwen3-1.7b-q4"     # 정밀
DEFAULT_GEN_MODEL = str(_setting("generation.model", FAST_GEN_MODEL,
                                 "SIMPLERAG_GEN_MODEL", str)).strip().lower()
if DEFAULT_GEN_MODEL not in GEN_MODELS:
    # yaml 값은 settings 검사가 먼저 막는다. 여기는 환경변수 오타를 시작할 때 알리기 위한 것
    from .settings import ConfigError as _ConfigError
    raise _ConfigError("SIMPLERAG_GEN_MODEL={!r} — {} 중 하나여야 합니다".format(
        DEFAULT_GEN_MODEL, " | ".join(GEN_MODELS)))

GEN_THREADS = 8             # prefill 이 8스레드에서 가장 빠름(REPORT §6)
GEN_N_CTX = 2048            # top-3 x 128토큰이면 충분. 크게 잡을수록 손해
GEN_N_BATCH = 512
GEN_FLASH_ATTN = True       # CPU 경로. prefill 이득 > decode 손해 (RAG 는 prefill 지배)
# 답변 토큰 상한. config.yaml generation.max_tokens / 환경변수 SIMPLERAG_GEN_MAX_TOKENS 로 바꿀 수 있다.
#   계획서 1-2: 150자 넘는 답의 Faithfulness 가 낮아(0.622 vs 0.719, REPORT §36) 200 → 120 을 실험한다.
GEN_MAX_TOKENS = _setting("generation.max_tokens", 200, "SIMPLERAG_GEN_MAX_TOKENS")
# 시스템 지시문 선택. v0 = 채택 프롬프트(종전과 글자까지 동일), v4 = 계획서 1-3 실험
#   ("질문의 답을 첫 문장에 바로, 1~2문장"). config.yaml generation.prompt / SIMPLERAG_PROMPT
PROMPT = str(_setting("generation.prompt", "v0", "SIMPLERAG_PROMPT", str)).strip().lower()
if PROMPT not in ("v0", "v4"):
    # yaml 값은 settings 검사가 먼저 막는다. 여기는 환경변수 오타를 시작할 때 알리기 위한 것
    from .settings import ConfigError as _ConfigError
    raise _ConfigError("SIMPLERAG_PROMPT={!r} — v0 | v4 중 하나여야 합니다".format(PROMPT))
GEN_TEMPERATURE = 0.0

# ── 생성 백엔드 (CPU / iGPU) ─────────────────────────
# 내장 GPU(Vulkan)로 prefill 을 넘기면 LLM 구간이 2.25→0.80초(506문항 실측,
# REPORT §31). 단 PC 마다 iGPU 유무·성능이 달라 **첫 실행 때 재서 고른다**
# (generate/backend.py). 결과는 BACKEND_CACHE_PATH 에 저장된다.
#   auto   — 재서 iGPU 가 GPU_MIN_SPEEDUP 배 이상 빠르면 iGPU (기본)
#   cpu    — 항상 CPU (종전 동작)
#   vulkan — iGPU 강제(쓸 수 없으면 CPU 로 폴백하고 이유를 알린다)
GEN_BACKEND = os.environ.get("SIMPLERAG_GEN_BACKEND", "auto").strip().lower()
# Vulkan 판 llama.cpp DLL 폴더(프로그램 폴더 기준). CPU 판은 llama_cpp 패키지 안에 있다.
#   Vulkan 판은 vulkan-1.dll 을 직접 링크해 CPU 판을 대신할 수 없다(런타임 없는
#   PC 에서 적재 자체가 실패) — 두 벌을 모두 동봉한다(REPORT §32).
VULKAN_LIB_DIR = os.environ.get(
    "SIMPLERAG_VULKAN_LIB", os.path.join(APP_DIR, "runtime", "llama_vulkan"))
GEN_GPU_LAYERS = -1         # 전 레이어. decode 는 CPU 와 같아 나눌 이유가 없다(§31.3)
# iGPU 에서는 flash_attn 을 끈다 — 속도 차이 0, 정답률 +3/+5문항(비유의, §31.12).
# CPU 에서는 켜는 쪽이 prefill 1.6배라 위의 GEN_FLASH_ATTN 을 그대로 쓴다.
GEN_FLASH_ATTN_GPU = False
# iGPU 는 답변이 CPU 와 달라지고 정답률이 조금 내려가는 신호가 있어(§31.12)
# 이만큼 확실히 빠를 때만 쓴다. Iris Xe 는 약 3배라 여유 있게 넘는다.
GPU_MIN_SPEEDUP = 1.3
BACKEND_PROBE_TIMEOUT = 180  # 초. 새 PC 첫 실행은 셰이더 컴파일로 10초 이상 걸린다
BACKEND_CACHE_PATH = os.path.join(ROOT, "gen_backend.json")

# 인덱싱 체크포인트 주기(청크 단위). 발열로 처리량이 하락하고 작업이 길어지므로
# 중단·재개가 가능해야 한다(설계서 결정10).
CHECKPOINT_EVERY = 500


#------------------------------------------------------------------
# 생성 모델 파일 경로
#=> 별칭을 실제 GGUF 경로로 바꾼다.
#
# -in: alias = GEN_MODELS 의 키
#
# -out: path = 모델 파일 절대경로
# -out: error = 알 수 없는 별칭이면 KeyError
#------------------------------------------------------------------
def gen_model_path(alias):
    return os.path.join(MODELS_DIR, GEN_MODELS[alias])
