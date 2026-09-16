#------------------------------------------------------------------
# config.yaml 설정 + 머리글 재부착 버그 회귀 테스트 (REPORT §35)
#=> 검색·청킹 계수를 config.yaml 로 뺀 것과, 표 머리글 재부착 상한이 설계대로인지 본다.
#   모델은 토크나이저만, 인덱스는 전혀 쓰지 않는다.
#
#   확인하는 것
#    A. 설정 읽기 — 저장소 config.yaml 이 코드 기본값과 같다(동작 불변) / 값 적용 /
#       환경변수 우선 / 오타·범위·타입 오류 / 경고
#    B. 검색 — dense·BM25 후보 수를 따로 쓰고, bm25_top_k=0 이면 BM25 를 건너뛴다 / RRF k
#    C. 청킹 — 거대한 머리글은 붙이지 않고 정상 머리글은 붙인다(버그 재현 포함)
#    D. 인덱스 — 청킹 설정이 기록과 다르면 증분을 거부한다
#
#   실행: .venv/Scripts/python.exe bench/test_config_yaml.py
#------------------------------------------------------------------

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                    # noqa: E402

FAILED = []
TOTAL = [0]
PY = sys.executable
KEYS = ["CHUNK_TOKENS", "CHUNK_OVERLAP", "CHUNK_MODE", "MIN_CHUNK_TOKENS",
        "TABLE_HEADER_MAX_TOKENS", "DENSE_TOP_K", "BM25_TOP_K", "RRF_K", "BM25_K1",
        "BM25_B", "DEDUP_EVIDENCE", "RERANK", "RERANK_POOL", "TOP_K", "GEN_MAX_TOKENS", "PROMPT"]


#------------------------------------------------------------------
# 단언 도우미 — 실패해도 멈추지 않고 끝까지 돌린다
#
# -in: name = 테스트 이름
# -in: cond = 참이어야 하는 조건
# -in: note = 실패 시 함께 출력할 설명
#
# -out: 없음
# -out: error = 없음 (실패는 FAILED 에 쌓는다)
#------------------------------------------------------------------
def check(name, cond, note=""):
    TOTAL[0] += 1
    print(("  OK   %s" if cond else "  FAIL %s  " + str(note)) % name)
    if not cond:
        FAILED.append(name)


#------------------------------------------------------------------
# 새 프로세스에서 config 를 읽어 값 받아 오기
#=> config 는 import 할 때 한 번 읽으므로, 조건마다 새 프로세스를 띄운다.
#
# -in: yaml_text = 쓸 config.yaml 내용(None 이면 SIMPLERAG_CONFIG 를 그대로 둔다)
# -in: env       = 추가 환경변수 dict
#
# -out: (returncode, 값 dict 또는 None, stderr 문자열)
# -out: error = 없음
#------------------------------------------------------------------
def load_in_child(yaml_text=None, env=None, tmp=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("SIMPLERAG_")}
    # 파이프로 받는 stderr 가 cp949 가 되면 한글 오류문이 깨져 검사가 헛돈다 — UTF-8 고정
    e["PYTHONIOENCODING"] = "utf-8"
    e.update(env or {})
    if yaml_text is not None:
        path = os.path.join(tmp, "cfg_%d.yaml" % TOTAL[0])
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        e["SIMPLERAG_CONFIG"] = path
    code = ("import json,sys; sys.path.insert(0,'src'); from simplerag import config as c; "
            "print(json.dumps({k: getattr(c, k) for k in %r} | "
            "{'CONFIG_PATH': c.CONFIG_PATH, 'CONFIG_WARNINGS': c.CONFIG_WARNINGS}))" % KEYS)
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=e)
    vals = None
    if r.returncode == 0:
        vals = json.loads(r.stdout.strip().splitlines()[-1])
    return r.returncode, vals, r.stderr


def test_settings(tmp):
    print("\n[A. 설정 읽기]")
    rc, d0, err = load_in_child(env={"SIMPLERAG_CONFIG": "-"})
    check("SIMPLERAG_CONFIG=- → 코드 기본값", rc == 0 and d0["CONFIG_PATH"] is None, err)

    rc, repo, err = load_in_child()
    check("저장소 config.yaml 을 찾는다", rc == 0 and str(repo["CONFIG_PATH"]).endswith("config.yaml"),
          (rc, err))
    same = rc == 0 and all(repo[k] == d0[k] for k in KEYS)
    check("저장소 config.yaml 값 = 코드 기본값(동작 불변)", same,
          {k: (repo[k], d0[k]) for k in KEYS if rc == 0 and repo[k] != d0[k]})
    check("기본값(128/16/structured/16/10/10/60/1.5/0.75/풀 10/3) — 풀은 §35.9 에서 5→10",
          d0 and (d0["CHUNK_TOKENS"], d0["CHUNK_OVERLAP"], d0["CHUNK_MODE"], d0["MIN_CHUNK_TOKENS"],
                  d0["DENSE_TOP_K"], d0["BM25_TOP_K"], d0["RRF_K"], d0["BM25_K1"], d0["BM25_B"],
                  d0["RERANK_POOL"], d0["TOP_K"], d0["DEDUP_EVIDENCE"], d0["RERANK"])
          == (128, 16, "structured", 16, 10, 10, 60, 1.5, 0.75, 10, 3, True, "auto"), d0)

    custom = """
chunk:
  tokens: 256
  overlap:
  table_header_max_tokens: 32
retrieval:
  dense_top_k: 20
  bm25_top_k: 5
  rrf_k: 30
  bm25_k1: 1.2
  bm25_b: 0.5
  dedup: false
rerank:
  mode: off
  pool: 8
generation:
  top_k: 4
  max_tokens: 120
"""
    rc, v, err = load_in_child(custom, tmp=tmp)
    ok = rc == 0 and (v["CHUNK_TOKENS"], v["CHUNK_OVERLAP"], v["TABLE_HEADER_MAX_TOKENS"],
                      v["DENSE_TOP_K"], v["BM25_TOP_K"], v["RRF_K"], v["BM25_K1"], v["BM25_B"],
                      v["DEDUP_EVIDENCE"], v["RERANK"], v["RERANK_POOL"], v["TOP_K"], v["GEN_MAX_TOKENS"]) \
        == (256, 32, 32, 20, 5, 30.0, 1.2, 0.5, False, "0", 8, 4, 120)
    check("값 적용 — overlap 비우면 tokens/8, YAML off(bool) → '0', max_tokens 120", ok, (rc, v, err))
    check("답변 토큰 상한 기본값 200(계획서 1-2 전)", d0 and d0["GEN_MAX_TOKENS"] == 200, d0)

    rc, v, err = load_in_child("generation:\n  max_tokens: 150\n", env={"SIMPLERAG_GEN_MAX_TOKENS": "120"}, tmp=tmp)
    check("SIMPLERAG_GEN_MAX_TOKENS 가 config.yaml 보다 우선(120)",
          rc == 0 and v["GEN_MAX_TOKENS"] == 120, (rc, v, err))

    check("시스템 지시문 기본값 v0(계획서 1-3 전)", d0 and d0["PROMPT"] == "v0", d0)
    rc, v, err = load_in_child("generation:\n  prompt: V4\n", tmp=tmp)
    check("generation.prompt: V4 → v4(대소문자 무시)", rc == 0 and v["PROMPT"] == "v4", (rc, v, err))
    rc, v, err = load_in_child("generation:\n  prompt: v4\n", env={"SIMPLERAG_PROMPT": "v0"}, tmp=tmp)
    check("SIMPLERAG_PROMPT 가 config.yaml 보다 우선(v0)", rc == 0 and v["PROMPT"] == "v0", (rc, v, err))
    rc, v, err = load_in_child(env={"SIMPLERAG_CONFIG": "-", "SIMPLERAG_PROMPT": "v9"})
    check("SIMPLERAG_PROMPT 오타 → 시작할 때 오류", rc != 0 and "v0 | v4" in err, (rc, err[-200:]))

    rc, v, err = load_in_child("chunk:\n  tokens: 256\n", env={"SIMPLERAG_CHUNK_TOKENS": "200"}, tmp=tmp)
    check("환경변수가 config.yaml 보다 우선(200, 겹침 25)",
          rc == 0 and v["CHUNK_TOKENS"] == 200 and v["CHUNK_OVERLAP"] == 25, (rc, v, err))

    rc, v, err = load_in_child("rerank:\n  pool: 2\n", tmp=tmp)
    check("pool < top_k → 경고(멈추지 않음)", rc == 0 and any("pool" in w for w in v["CONFIG_WARNINGS"]),
          (rc, v and v["CONFIG_WARNINGS"], err))

    bad = [
        ("모르는 키(오타) + 힌트", "chunk:\n  token: 256\n", ["chunk.token", "tokens"]),
        ("모르는 섹션", "retreival:\n  dense_top_k: 5\n", ["retreival", "retrieval"]),
        ("범위 밖(청크 5000)", "chunk:\n  tokens: 5000\n", ["16~510"]),
        ("타입 오류(문자)", "retrieval:\n  dense_top_k: 열개\n", ["정수"]),
        ("bool 을 정수로", "generation:\n  top_k: yes\n", ["정수"]),
        ("답변 토큰 상한 범위 밖", "generation:\n  max_tokens: 4096\n", ["16~1024"]),
        ("모르는 프롬프트 이름", "generation:\n  prompt: v3\n", ["v0 | v4"]),
        ("겹침 ≥ 크기", "chunk:\n  tokens: 64\n  overlap: 64\n", ["겹침"]),
        ("rerank.mode 오타", "rerank:\n  mode: always\n", ["auto | on | off"]),
    ]
    for name, text, needles in bad:
        rc, v, err = load_in_child(text, tmp=tmp)
        check("오류로 멈춤 — %s" % name, rc != 0 and all(n in err for n in needles), (rc, err[-300:]))

    rc, v, err = load_in_child(env={"SIMPLERAG_CONFIG": os.path.join(tmp, "없는파일.yaml")})
    check("SIMPLERAG_CONFIG 파일 없음 → 오류", rc != 0 and "없습니다" in err, err[-200:])


#------------------------------------------------------------------
# 가짜 검색 구성요소 — 요청된 후보 수만 기록한다
#------------------------------------------------------------------
class _Emb:
    tokenizer = None

    def embed_query(self, q):
        return [0.0]


class _Store:
    def __init__(self):
        self.asked = []

    def search(self, qv, k):
        self.asked.append(k)
        return [(i, 1.0, {"text": "d%d" % i}) for i in range(k)]


class _Bm25:
    def __init__(self):
        self.asked = []

    def search(self, q, tok, k):
        self.asked.append(k)
        return list(range(100, 100 + k))


def test_retrieval():
    print("\n[B. 검색 계수 적용]")
    from simplerag.retrieve.hybrid import HybridRetriever, rrf
    saved = {k: getattr(config, k) for k in ("DENSE_TOP_K", "BM25_TOP_K", "RRF_K", "DEDUP_EVIDENCE")}
    try:
        config.DENSE_TOP_K, config.BM25_TOP_K, config.DEDUP_EVIDENCE = 7, 4, False
        st, bm = _Store(), _Bm25()
        r = HybridRetriever(_Emb(), st, bm)
        r._fetch_payloads = lambda ids: {i: {"text": "b%d" % i} for i in ids}
        r._search("q", top_k=3)
        check("dense 에 dense_top_k(7), BM25 에 bm25_top_k(4)", st.asked == [7] and bm.asked == [4],
              (st.asked, bm.asked))

        config.BM25_TOP_K = 0
        st, bm = _Store(), _Bm25()
        r = HybridRetriever(_Emb(), st, bm)
        chunks, _ = r._search("q", top_k=3)
        check("bm25_top_k=0 → BM25 호출 안 함, dense 단독", bm.asked == [] and len(chunks) == 3,
              (bm.asked, len(chunks)))

        config.RRF_K = 30
        s = dict(rrf([["a", "b"]]))
        check("RRF k 적용 — 1위 점수 1/(30+1)", abs(s["a"] - 1 / 31) < 1e-12, s)
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def test_chunk_header():
    print("\n[C. 표 머리글 재부착 상한]")
    from tokenizers import Tokenizer
    from simplerag.chunk import split_structured
    tok = Tokenizer.from_file(os.path.join(config.MODEL_DIR, "tokenizer.json"))
    tok.no_truncation()
    tok.no_padding()
    n = lambda s: len(tok.encode(s, add_special_tokens=False).ids)

    def table(header):
        rows = ["| 항목%d | 경조사 지원 내용 설명 %d번째 행입니다 |" % (i, i) for i in range(60)]
        return "\n".join([header] + rows)

    huge = "| " + " | ".join("필드코드이름%d" % i for i in range(250)) + " |"
    normal = "| 구분 | 지원 내용 |"
    saved = config.TABLE_HEADER_MAX_TOKENS
    try:
        config.TABLE_HEADER_MAX_TOKENS = 10 ** 6            # 종전 동작(상한 없음) — 버그 재현
        old = split_structured(table(huge), tok, title="시험문서")
        config.TABLE_HEADER_MAX_TOKENS = 64
        new = split_structured(table(huge), tok, title="시험문서")
        nor = split_structured(table(normal), tok, title="시험문서")
    finally:
        config.TABLE_HEADER_MAX_TOKENS = saved

    check("버그 재현 — 상한 없으면 거대 머리글(%d토큰)이 조각마다 붙음" % n(huge),
          sum(1 for c in old if huge in c) >= 2 and max(map(n, old)) > 1000,
          (sum(1 for c in old if huge in c), max(map(n, old))))
    check("수정 — 상한 64 면 거대 머리글을 조각에 다시 붙이지 않음",
          sum(1 for c in new if huge in c) == 0, sum(1 for c in new if huge in c))
    over = [n(c) for c in new if huge[:30] not in c and n(c) > config.CHUNK_TOKENS + 64]
    check("수정 — 머리글 줄 자체 조각을 빼면 청크 ≤ 크기+상한", over == [], over)
    with_hdr = sum(1 for c in nor[1:] if normal in c)
    check("정상 머리글(%d토큰)은 뒤 조각에도 붙음(%d/%d)" % (n(normal), with_hdr, len(nor) - 1),
          len(nor) >= 2 and with_hdr == len(nor) - 1, (with_hdr, len(nor)))


def test_index_guard():
    print("\n[D. 인덱스 청킹 설정 검사]")
    from simplerag.index.indexer import check_chunk_params, chunk_params
    logs = []
    st = {"docs": {"a": {}}, "next_id": 1}
    check_chunk_params(st, rebuild=False, log=logs.append)
    check("기록 없는 옛 인덱스 → 경고만, 기록하지 않음",
          "chunk_params" not in st and any("기록이 없습니다" in m for m in logs), (st, logs))

    st = {"docs": {"a": {}}, "next_id": 1, "chunk_params": dict(chunk_params(), tokens=256)}
    try:
        check_chunk_params(st, rebuild=False, log=logs.append)
        raised = ""
    except RuntimeError as e:
        raised = str(e)
    check("기록과 다르면 증분 거부 + --rebuild 안내", "tokens 256→" in raised and "--rebuild" in raised, raised)

    st = {"docs": {"a": {}}, "next_id": 1, "chunk_params": dict(chunk_params(), tokens=256)}
    check_chunk_params(st, rebuild=True, log=logs.append)
    check("--rebuild 면 지금 설정으로 기록", st["chunk_params"] == chunk_params())

    st = {"docs": {}, "next_id": 0}
    check_chunk_params(st, rebuild=False, log=logs.append)
    check("빈 인덱스면 지금 설정으로 기록", st.get("chunk_params") == chunk_params())

    st = {"docs": {"a": {}}, "next_id": 1, "chunk_params": chunk_params()}
    check_chunk_params(st, rebuild=False, log=logs.append)
    check("같으면 통과", st["chunk_params"] == chunk_params())


def main():
    tmp = tempfile.mkdtemp(prefix="simplerag_cfg_")
    test_settings(tmp)
    test_retrieval()
    test_chunk_header()
    test_index_guard()
    print("\n%d개 중 %d개 실패" % (TOTAL[0], len(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
