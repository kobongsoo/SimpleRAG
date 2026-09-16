#------------------------------------------------------------------
# config.yaml 읽기·검증 (REPORT §35)
#=> 검색·청킹 계수(청크 크기·겹침·검색 후보 수·RRF k·리랭커 입력 수·sLLM 근거 수 등)를
#   코드를 고치지 않고 바꿀 수 있게 config.yaml 로 뺐다.
#    1) 파일 찾기: SIMPLERAG_CONFIG → <데이터 루트>/config.yaml → <프로그램 폴더>/config.yaml
#       (SIMPLERAG_CONFIG=- 이면 파일을 쓰지 않는다 — 코드 기본값으로 고정할 때)
#    2) 스키마 검사: 모르는 섹션·키, 잘못된 타입, 범위 밖 값은 ConfigError
#    3) 평탄화: {"chunk.tokens": 128, ...} 로 돌려준다 — 환경변수와의 우선순위 조립은
#       config.py 가 한다(환경변수 > config.yaml > 코드 기본값)
#
#   왜 오류로 멈추는가 — 오타("token:")를 조용히 무시하면 기본값으로 수십 분짜리 인덱싱이
#   다 돌고 나서야 설정이 안 먹었음을 안다. 시작할 때 무엇을 고칠지 알려 주는 편이 싸다.
#
#   이 모듈은 config.py 보다 먼저 import 되므로 config 에 의존하지 않는다.
#------------------------------------------------------------------

import difflib
import os


class ConfigError(ValueError):
    """config.yaml 이 잘못됐거나 설정값끼리 모순될 때."""


#------------------------------------------------------------------
# 정수 검사기 만들기
#
# -in: lo, hi = 허용 범위(양끝 포함)
#
# -out: check(v) → (정규화된 값, 오류문 또는 None)
# -out: error = 없음
#------------------------------------------------------------------
def _int(lo, hi):
    # 파이썬에서 True 는 int 이므로 명시적으로 막는다(yes/no 가 bool 로 읽힌다)
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return v, "정수여야 합니다"
        if not lo <= v <= hi:
            return v, "{}~{} 사이여야 합니다".format(lo, hi)
        return v, None
    return check


#------------------------------------------------------------------
# 실수 검사기 만들기
#
# -in: lo, hi     = 허용 범위
# -in: lo_open    = True 면 lo 는 포함하지 않는다(0 초과)
#
# -out: check(v) → (float 값, 오류문 또는 None)
# -out: error = 없음
#------------------------------------------------------------------
def _num(lo, hi, lo_open=False):
    # 정수로 적어도(1) 실수 계수로 받아들인다
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return v, "숫자여야 합니다"
        if (v <= lo if lo_open else v < lo) or v > hi:
            return v, "{} {} ~ {} 이하여야 합니다".format(lo, "초과" if lo_open else "이상", hi)
        return float(v), None
    return check


#------------------------------------------------------------------
# 선택지 검사기 만들기
#
# -in: *choices = 허용 문자열
#
# -out: check(v) → (소문자 문자열, 오류문 또는 None)
# -out: error = 없음
#------------------------------------------------------------------
def _choice(*choices):
    def check(v):
        s = str(v).strip().lower()
        if s not in choices:
            return v, "{} 중 하나여야 합니다".format(" | ".join(choices))
        return s, None
    return check


#------------------------------------------------------------------
# 참/거짓 검사
#
# -in: v = 값
#
# -out: (bool, 오류문 또는 None)
# -out: error = 없음
#------------------------------------------------------------------
def _bool(v):
    if isinstance(v, bool):
        return v, None
    return v, "true 또는 false 여야 합니다"


#------------------------------------------------------------------
# 리랭킹 모드 검사 — YAML 이 on/off 를 bool 로 읽는 것까지 받아 준다
#
# -in: v = 값 (auto | on | off, 또는 true/false)
#
# -out: ("auto" | "1" | "0", 오류문 또는 None) — backend.rerank_enabled 가 읽는 형태
# -out: error = 없음
#------------------------------------------------------------------
def _rerank_mode(v):
    if v is True:
        return "1", None
    if v is False:
        return "0", None
    s = str(v).strip().lower()
    if s in ("auto",):
        return "auto", None
    if s in ("on", "1", "true", "yes"):
        return "1", None
    if s in ("off", "0", "false", "no"):
        return "0", None
    return v, "auto | on | off 중 하나여야 합니다"


#------------------------------------------------------------------
# 빈 값(null)도 허용하는 검사기로 감싸기
#
# -in: inner = 원래 검사기
#
# -out: check(v)
# -out: error = 없음
#------------------------------------------------------------------
def _optional(inner):
    def check(v):
        return (None, None) if v is None else inner(v)
    return check


# 섹션 → 키 → 검사기. 여기에 없는 키는 오타로 보고 오류를 낸다.
SCHEMA = {
    "chunk": {
        "tokens": _int(16, 510),                  # e5 입력 상한 512 − 특수토큰 2
        "overlap": _optional(_int(0, 509)),
        "mode": _choice("structured", "fixed"),
        "min_tokens": _int(1, 510),
        "table_header_max_tokens": _int(0, 510),
    },
    "retrieval": {
        "dense_top_k": _int(1, 1000),
        "bm25_top_k": _int(0, 1000),
        "rrf_k": _num(0, 100000, lo_open=True),
        "bm25_k1": _num(0, 100, lo_open=True),
        "bm25_b": _num(0, 1),
        "dedup": _bool,
    },
    "rerank": {
        "mode": _rerank_mode,
        "pool": _int(1, 100),
    },
    "generation": {
        "top_k": _int(1, 20),
        "max_tokens": _int(16, 1024),             # 답변 토큰 상한 — n_ctx(2048) 안에 근거 자리를 남긴다
        "prompt": _choice("v0", "v4"),            # 시스템 지시문(generate/prompts.py PROMPTS)
    },
}


#------------------------------------------------------------------
# 오타 힌트
#
# -in: name    = 사용자가 쓴 이름
# -in: options = 올바른 이름들
#
# -out: " (혹시 'tokens'?)" 또는 ""
# -out: error = 없음
#------------------------------------------------------------------
def _hint(name, options):
    close = difflib.get_close_matches(str(name), list(options), n=1, cutoff=0.6)
    return " (혹시 '{}'?)".format(close[0]) if close else ""


#------------------------------------------------------------------
# 설정 파일 찾기
#=> 앞에서부터 처음 있는 파일을 쓴다. 청킹 설정은 인덱스와 짝이므로 데이터 루트를
#   프로그램 폴더보다 먼저 본다(SIMPLERAG_HOME 으로 인덱스를 옮기면 설정도 따라간다).
#
# -in: root    = 데이터 루트(config.ROOT)
# -in: app_dir = 프로그램 폴더(config.APP_DIR)
#
# -out: 파일 경로 또는 None(파일 없음 / SIMPLERAG_CONFIG=-)
# -out: error = SIMPLERAG_CONFIG 로 지정한 파일이 없으면 ConfigError
#------------------------------------------------------------------
def find_config(root, app_dir):
    env = os.environ.get("SIMPLERAG_CONFIG")
    if env is not None and env.strip():
        if env.strip() == "-":
            return None
        path = os.path.abspath(env.strip())
        if not os.path.isfile(path):
            raise ConfigError("SIMPLERAG_CONFIG 로 지정한 파일이 없습니다: {}".format(path))
        return path
    for d in (root, app_dir):
        path = os.path.join(d, "config.yaml")
        if os.path.isfile(path):
            return path
    return None


#------------------------------------------------------------------
# config.yaml 읽기 (핵심)
#    1) 파일을 찾는다(없으면 빈 설정)
#    2) YAML 로 읽는다(UTF-8, BOM 허용 — EditPlus 로 저장해도 읽히게)
#    3) 섹션·키·값을 스키마로 검사하고, 오류를 모두 모아 한 번에 알린다
#
# -in: root, app_dir = find_config() 와 같음
#
# -out: (path, flat) = (읽은 파일 경로 또는 None, {"섹션.키": 값})
#        값을 비워 둔 키(null)는 flat 에 넣지 않는다 → 코드 기본값을 쓴다
# -out: error = 파일 형식 오류·모르는 키·타입/범위 오류는 ConfigError(파일 경로 포함)
#------------------------------------------------------------------
def load_settings(root, app_dir):
    path = find_config(root, app_dir)
    if path is None:
        return None, {}

    try:
        import yaml
    except ImportError as e:
        raise ConfigError("config.yaml 을 읽으려면 PyYAML 이 필요합니다: {}".format(e))

    try:
        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError("{} — YAML 형식 오류: {}".format(path, e))
    except OSError as e:
        raise ConfigError("{} — 읽을 수 없습니다: {}".format(path, e))

    if data is None:
        return path, {}
    if not isinstance(data, dict):
        raise ConfigError("{} — 최상위에는 섹션(chunk / retrieval / rerank / generation)이 "
                          "와야 합니다".format(path))

    flat, errors = {}, []
    for sec, body in data.items():
        if sec not in SCHEMA:
            errors.append("알 수 없는 섹션 '{}'{}".format(sec, _hint(sec, SCHEMA)))
            continue
        if body is None:
            continue
        if not isinstance(body, dict):
            errors.append("'{}' 아래에는 '키: 값' 줄이 와야 합니다".format(sec))
            continue
        spec = SCHEMA[sec]
        for key, val in body.items():
            if key not in spec:
                errors.append("알 수 없는 키 '{}.{}'{}".format(sec, key, _hint(key, spec)))
                continue
            if val is None:
                continue
            norm, err = spec[key](val)
            if err:
                errors.append("'{}.{}: {}' — {}".format(sec, key, val, err))
            elif norm is not None:
                flat[sec + "." + key] = norm

    if errors:
        raise ConfigError("{}\n  - {}".format(path, "\n  - ".join(errors)))
    return path, flat


#------------------------------------------------------------------
# 조립된 최종값끼리의 모순 검사
#=> 환경변수까지 합친 뒤에만 알 수 있는 관계를 본다.
#   고칠 수밖에 없는 모순은 ConfigError, 동작은 하지만 의도와 다를 수 있는 것은 경고.
#
# -in: tokens, overlap, min_tokens, header_max = 청킹 최종값
# -in: top_k, pool                               = sLLM 근거 수, 리랭커 입력 수
# -in: dense_k, bm25_k                           = 1차 후보 수
#
# -out: warnings = 경고 문장 리스트
# -out: error = 청크 크기 범위 밖, overlap ≥ tokens, min_tokens > tokens 면 ConfigError
#------------------------------------------------------------------
def check_resolved(tokens, overlap, min_tokens, header_max, top_k, pool, dense_k, bm25_k):
    errors = []
    if not 16 <= tokens <= 510:
        errors.append("청크 크기 {} — 16~510 이어야 합니다".format(tokens))
    if not 0 <= overlap < tokens:
        errors.append("겹침 {} — 0 이상, 청크 크기({}) 미만이어야 합니다".format(overlap, tokens))
    if min_tokens > tokens:
        errors.append("최소 조각 {} — 청크 크기({}) 이하여야 합니다".format(min_tokens, tokens))
    if errors:
        raise ConfigError("설정값 모순\n  - " + "\n  - ".join(errors))

    warnings = []
    if pool < top_k:
        warnings.append("rerank.pool({}) < generation.top_k({}) — 리랭커 입력은 top_k 로 "
                        "올려서 쓴다(재정렬 여지 없음)".format(pool, top_k))
    if dense_k + bm25_k < max(pool, top_k):
        warnings.append("dense_top_k + bm25_top_k({}) < 필요한 후보 수({}) — 근거가 모자랄 수 "
                        "있다".format(dense_k + bm25_k, max(pool, top_k)))
    if header_max > tokens:
        warnings.append("table_header_max_tokens({}) > 청크 크기({}) — 표 청크가 크기의 두 배 "
                        "넘게 커질 수 있다".format(header_max, tokens))
    return warnings
