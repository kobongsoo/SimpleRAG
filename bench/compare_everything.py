#------------------------------------------------------------------
# 문서 검색 비교 — MoAI-S 탐색기 찾기 vs Everything (메타데이터 검색 설계서 §16-2 · 8단계)
#=> 같은 문서(D:\분류함) · 같은 질문으로 정답 문서가 몇 번째에 나오는지 잰다.
#    · MoAI-S : 자연어 그대로(형태 A) — dist-rust 상주 워커 /find, 화면 순서로 순위
#    · Everything A : 자연어 그대로 es.exe 에 넣음(낱말마다 파일 이름 · 경로 AND)
#    · Everything B : 숙련자 문법 — 메타 질의는 남은 주제어 + ext:(형식) + dm:(기간),
#                     본문 질의는 content:핵심어(본문 읽기, 느림), 파일명 질의는 A 와 같음
#   질문 세트: 메타 30(bench/meta_golden.jsonl) · 본문 30(dataset/golden_dataset.jsonl 앞쪽, 인덱스에 있는 문서)
#              · 파일명 10(bench/name_golden.jsonl)
#   결과: results/search_compare/compare_<시각>.json (문항별 순위 · 시간 · 쓴 질의)
#
#   사용: .venv\Scripts\python.exe bench\compare_everything.py [--body N] [--only meta,body,name]
#   ⚠️ Everything 이 떠 있어야 한다(트레이). dist-rust 의 RAGSearchBox 는 꺼 둔다(인덱스 잠금).
#------------------------------------------------------------------

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from test_meta_golden import Worker, rank_of  # noqa: E402

DIST = os.path.join(_ROOT, "dist-rust")
SCOPE = r"D:\분류함"
ES = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
                  "voidtools.Everything.Cli_Microsoft.Winget.Source_8wekyb3d8bbwe", "es.exe")
OUT_DIR = os.path.join(_ROOT, "results", "search_compare")
EXT = {"excel": "xls;xlsx", "ppt": "ppt;pptx", "hwp": "hwp;hwpx", "pdf": "pdf", "word": "doc;docx"}
JOSA = re.compile(r"(?:에서|으로|에게|하고|이랑|이나|부터|까지|에는|에서는|은|는|이|가|을|를|의|에|로|와|과|도|만)$")
VERBY = re.compile(r"(?:나요|가요|세요|니다|하면|려면|으면|되고|되면|되나|인가|는지|는데|면서|했다|하나|하는|한다|할|된|하고|받으|있는|없는|어야|아야|해야|궁금)$")
STOP = {"회사", "직원", "사내", "본인", "주는", "명이", "최대", "최소", "가능", "어떻게", "무엇", "얼마", "얼마인가요", "있나요", "되나요", "하나요", "경우", "때", "관련", "대한", "어떤", "무엇인가요"}


#------------------------------------------------------------------
# Everything 검색 한 번 — es.exe 결과 경로 목록(이름순 = Everything 기본 화면 순서)
#
# -in: terms = es 인자 목록 / timeout = 초
#
# -out: (경로 목록, 걸린 초) — 시간 초과면 ([], timeout)
# -out: error = 없음
#------------------------------------------------------------------
def es_search(terms, timeout=150):
    out = os.path.join(tempfile.gettempdir(), "es_cmp.txt")
    t0 = time.perf_counter()
    try:
        subprocess.run([ES, "-path", SCOPE, "-n", "50"] + terms + ["-export-txt", out],
                       capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], float(timeout)
    sec = time.perf_counter() - t0
    try:
        paths = [l.strip() for l in open(out, encoding="utf-8-sig") if l.strip()]
    except OSError:
        paths = []
    # 폴더는 문서가 아니다
    return [p for p in paths if os.path.splitext(p)[1]], sec


#------------------------------------------------------------------
# 경로 목록에서 정답 순위 — 경로에 정답 글이 든 첫 항목(1부터)
#------------------------------------------------------------------
def rank_paths(paths, answers):
    keys = [a.lower() for a in answers]
    for i, p in enumerate(paths, 1):
        if any(k in p.lower() for k in keys):
            return i
    return None


#------------------------------------------------------------------
# 질문에서 숙련자가 칠 핵심 명사 — 검색기와 같은 핵심 낱말에서 동사 · 의문 꼴(지급되나요 · 신청하려면)을 빼고
#   질문에 나온 순서대로 n 개
#------------------------------------------------------------------
def key_words(q, n=2):
    from simplerag.retrieve.hybrid import keywords
    ws = []
    for w in keywords(q):
        if VERBY.search(w) or w in STOP or w in ws:
            continue
        ws.append(w)
    return ws[:n]


#------------------------------------------------------------------
# 메타 질의 → Everything 숙련자 문법(형태 B)
#=> MoAI-S 와 같은 해석기로 조건을 뽑아 Everything 문법으로 옮긴다(사람 조건은 Everything 1.4 에 없어 버림).
#
# -in: q = 질의 / P = (이름, 별칭, me, 규칙)
#
# -out: es 인자 목록
#------------------------------------------------------------------
def meta_to_es(q, P):
    from simplerag.meta import query as Q
    p = Q.parse(q, P[0], P[1], P[2], P[3], date(2026, 10, 29))
    terms = [w for w in p["query"].split() if w not in ("양식", "서식", "템플릿")]
    if p["format"]:
        terms.append("ext:" + EXT[p["format"]])
    if p["scanned"]:
        terms.append("ext:pdf")
    if p["date"]:
        lab = p["date"]["label"]
        m = re.match(r"(\d{4})년(?: (\d{1,2})월)?", lab)
        if m:
            # Everything 은 dm:2024-07 꼴(ISO). dm:2017/3 은 먹히지 않는다(실측)
            terms.append("dm:{}{}".format(m.group(1), "-{:02d}".format(int(m.group(2))) if m.group(2) else ""))
    if not terms:
        # 조건만 있는 질의 — 사람 이름이라도 파일 이름에서 찾는다
        terms = [pp["name"] for pp in p["people"]][:1] or ["*"]
    return terms


#------------------------------------------------------------------
# 질문 세트 읽기
#------------------------------------------------------------------
def load_sets(n_body, indexed):
    meta = [json.loads(l) for l in open(os.path.join(_HERE, "meta_golden.jsonl"), encoding="utf-8") if l.strip()]
    name = [json.loads(l) for l in open(os.path.join(_HERE, "name_golden.jsonl"), encoding="utf-8") if l.strip()]
    body = []
    for l in open(os.path.join(_ROOT, "dataset", "golden_dataset.jsonl"), encoding="utf-8"):
        d = json.loads(l)
        stems = [os.path.splitext(s)[0] for s in d.get("source_files", [])]
        # 분류함 인덱스에 그 문서(사본 포함)가 있는 문항만
        if stems and any(any(s.lower() in k for k in indexed) for s in stems):
            body.append({"id": d["id"], "kind": "본문", "q": d["user_input"], "answers": stems})
        if len(body) >= n_body:
            break
    return {"meta": meta, "body": body, "name": name}


#------------------------------------------------------------------
# 실행 — 세트마다 MoAI-S · Everything A · B
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--body", type=int, default=30)
    ap.add_argument("--only", default="meta,body,name")
    a = ap.parse_args()
    st = json.load(open(os.path.join(DIST, "index_state.json"), encoding="utf-8"))
    indexed = [k.lower() for k in st["docs"]]
    sets = load_sets(a.body, indexed)
    from simplerag.meta import query as Q
    from simplerag.meta.rules import load_rules
    db = os.path.join(DIST, "meta.sqlite")
    names, aliases, me = Q._names(db, DIST)
    P = (names, aliases, me, load_rules(DIST))
    w = Worker(os.path.join(DIST, "simplerag-rs.exe"), True)
    res = {}
    try:
        for sname in a.only.split(","):
            rows = []
            print("── {} ({}문항)".format(sname, len(sets[sname])), flush=True)
            for it in sets[sname]:
                t0 = time.perf_counter()
                docs, done = w.find(it["q"])
                moai = (rank_of(docs, it["answers"]), round(time.perf_counter() - t0, 3))
                pa, sa = es_search(it["q"].split(), 30)
                if sname == "meta":
                    tb = meta_to_es(it["q"], P)
                elif sname == "body":
                    tb = ['content:{}'.format(k) for k in key_words(it["q"])]  # 따옴표를 붙이면 es 가 빈 결과를 낸다(실측)
                else:
                    tb = it["q"].split()
                pb, sb = (pa, sa) if tb == it["q"].split() else es_search(tb)
                r = {"id": it["id"], "q": it["q"], "moai": moai[0], "moai_s": moai[1],
                     "ev_a": rank_paths(pa, it["answers"]), "ev_a_s": round(sa, 3),
                     "ev_b": rank_paths(pb, it["answers"]), "ev_b_s": round(sb, 3), "ev_b_q": " ".join(tb)}
                rows.append(r)
                print("  {:<5} MoAI {:>3}  EvA {:>3}  EvB {:>3}  | {}".format(
                    r["id"], r["moai"] or "-", r["ev_a"] or "-", r["ev_b"] or "-", r["ev_b_q"][:50]), flush=True)
            res[sname] = {"items": rows, "summary": summarize(rows)}
            print("   →", res[sname]["summary"], flush=True)
    finally:
        w.close()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "compare_{}.json".format(time.strftime("%Y%m%d_%H%M")))
    json.dump(res, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("저장:", path)
    return 0


#------------------------------------------------------------------
# 요약 — 도구마다 Top-1 · Top-10 · MRR · 시간 중앙값 · 최댓값
#------------------------------------------------------------------
def summarize(rows):
    n = len(rows)
    out = {"n": n}
    for k in ("moai", "ev_a", "ev_b"):
        rk = [r[k] for r in rows]
        sec = [r[k + "_s"] for r in rows]
        out[k] = {"top1": sum(1 for x in rk if x == 1), "top10": sum(1 for x in rk if x and x <= 10),
                  "mrr": round(sum(1.0 / x for x in rk if x and x <= 10) / n, 3),
                  "sec_med": round(statistics.median(sec), 2), "sec_max": round(max(sec), 2)}
    return out


if __name__ == "__main__":
    sys.exit(main())
