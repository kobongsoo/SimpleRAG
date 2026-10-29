#------------------------------------------------------------------
# 문서 검색 비교 2 — AnyTXT Searcher (메타데이터 검색 설계서 §16-2 · 8단계)
#=> compare_everything.py 와 같은 70문항을 AnyTXT(본문 전문 색인 검색기)에 넣어 정답 순위를 잰다.
#    · AnyTXT A : 자연어 그대로(낱말 AND)
#    · AnyTXT B : 숙련자 꼴 — 메타 질의는 남은 주제어 + 확장자 필터 + 수정일 범위,
#                 본문 질의는 핵심 명사 2개, 파일명 질의는 A 와 같음
#   MoAI-S 순위는 다시 재지 않고 compare_everything 결과(json)를 그대로 옮겨 적는다.
#   호출: AnyTXT 창의 HTTP 검색(베타) JSON-RPC — http://127.0.0.1:9920 ATRpcServer.Searcher.V1.GetResult
#   결과: results/search_compare/anytxt_<시각>.json
#
#   사용: .venv\Scripts\python.exe bench\compare_anytxt.py [--only meta,body,name]
#   ⚠️ AnyTXT 창이 떠 있어야 한다(옵션 > HTTP 검색 켜짐).
#------------------------------------------------------------------

import argparse
import calendar
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from compare_everything import DIST, EXT, OUT_DIR, SCOPE, key_words, load_sets, rank_paths, summarize  # noqa: E402

RPC = "http://127.0.0.1:9920"
# MoAI-S 순위를 가져올 이전 측정 결과(메타 · 파일명 / 본문 재측정)
MOAI_FILES = ["compare_meta_name_20261029.json", "compare_20261029_1922.json"]


#------------------------------------------------------------------
# AnyTXT 검색 한 번
#=> JSON-RPC GetResult 로 D:\분류함 안에서 찾고, 화면 순서대로 경로 목록을 돌려준다.
#    ⚠️ 한글을 \uXXXX 로 이스케이프하면 AnyTXT 가 400 을 낸다(실측) → ensure_ascii=False 로 보낸다
#
# -in: pattern = 검색어 / ext = 확장자 필터("*" 또는 "*.pdf;*.hwp" 꼴) / dm = (시작, 끝) 유닉스 초 또는 None
#
# -out: (경로 목록, 걸린 초)
# -out: error = 연결 · HTTP 오류는 예외 전파
#------------------------------------------------------------------
def at_search(pattern, ext="*", dm=None, limit=50):
    inp = {"pattern": pattern, "filterDir": SCOPE, "filterExt": ext,
           "lastModifyBegin": dm[0] if dm else 0, "lastModifyEnd": dm[1] if dm else 2147483647,
           "limit": limit, "offset": 0, "order": 0}
    body = json.dumps({"id": 1, "jsonrpc": "2.0", "method": "ATRpcServer.Searcher.V1.GetResult",
                       "params": {"input": inp}}, ensure_ascii=False).encode("utf-8")
    # Accept 헤더가 없으면 400 을 낸다(실측)
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json",
                                                          "Accept": "application/json"})
    t0 = time.perf_counter()
    res = json.loads(urllib.request.urlopen(req, timeout=300).read().decode("utf-8"))
    sec = time.perf_counter() - t0
    out = res["result"]["data"]["output"]
    # field 순서(fid, lastModify, size, file)를 믿지 않고 이름으로 찾는다
    i = out["field"].index("file")
    return [row[i] for row in out.get("files", [])], sec


#------------------------------------------------------------------
# 메타 질의 → AnyTXT 숙련자 꼴(형태 B)
#=> MoAI-S 와 같은 해석기로 조건을 뽑아 AnyTXT 가 받는 필터로 옮긴다
#    (사람 · 문서 유형 조건은 AnyTXT 에 없어 주제어만 남긴다).
#
# -in: q = 질의 / P = (이름, 별칭, me, 규칙)
#
# -out: (검색어, 확장자 필터, 수정일 범위 또는 None)
# -out: error = 없음
#------------------------------------------------------------------
def meta_to_at(q, P):
    from simplerag.meta import query as Q
    p = Q.parse(q, P[0], P[1], P[2], P[3], date(2026, 10, 29))
    terms = [w for w in p["query"].split() if w not in ("양식", "서식", "템플릿")]
    exts = []
    if p["format"]:
        exts = EXT[p["format"]].split(";")
    if p["scanned"]:
        exts = ["pdf"]
    dm = None
    if p["date"]:
        m = re.match(r"(\d{4})년(?: (\d{1,2})월)?", p["date"]["label"])
        if m:
            y = int(m.group(1))
            # 월이 있으면 그 달, 없으면 그 해 전체
            m1, m2 = (int(m.group(2)),) * 2 if m.group(2) else (1, 12)
            dm = (calendar.timegm((y, m1, 1, 0, 0, 0)),
                  calendar.timegm((y, m2, calendar.monthrange(y, m2)[1], 23, 59, 59)))
    if not terms:
        # 조건만 있는 질의 — 사람 이름이라도 넣는다(본문에 이름이 있을 수 있다)
        terms = [pp["name"] for pp in p["people"]][:1]
    return " ".join(terms), ";".join("*." + e for e in exts) or "*", dm


#------------------------------------------------------------------
# 이전 측정에서 MoAI-S 순위 · 시간 읽기
#=> 같은 문항을 MoAI-S 로 다시 돌리지 않으려고 compare_everything 결과를 읽어 온다.
#
# -in: 없음
#
# -out: {(세트, id): (순위, 초)}
# -out: error = 결과 파일이 없으면 OSError 전파
#------------------------------------------------------------------
def moai_ranks():
    out = {}
    for f in MOAI_FILES:
        d = json.load(open(os.path.join(OUT_DIR, f), encoding="utf-8"))
        for sname, v in d.items():
            for r in v["items"]:
                # 뒤 파일(본문 재측정)이 앞 파일 값을 덮는다
                out[(sname, r["id"])] = (r["moai"], r["moai_s"])
    return out


#------------------------------------------------------------------
# 실행 — 세트마다 AnyTXT A · B
#=> 문항마다 A(그대로) · B(숙련자 꼴)로 찾아 정답 순위를 매기고 json 으로 남긴다.
#
# -in: 없음 (명령줄 --only)
#
# -out: 0 = 정상 종료
# -out: error = AnyTXT 연결 실패는 예외 전파
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="meta,body,name")
    a = ap.parse_args()
    st = json.load(open(os.path.join(DIST, "index_state.json"), encoding="utf-8"))
    sets = load_sets(30, [k.lower() for k in st["docs"]])
    from simplerag.meta import query as Q
    from simplerag.meta.rules import load_rules
    names, aliases, me = Q._names(os.path.join(DIST, "meta.sqlite"), DIST)
    P = (names, aliases, me, load_rules(DIST))
    mo = moai_ranks()
    res = {}
    for sname in a.only.split(","):
        rows = []
        print("── {} ({}문항)".format(sname, len(sets[sname])), flush=True)
        for it in sets[sname]:
            pa, sa = at_search(it["q"])
            if sname == "meta":
                tb = meta_to_at(it["q"], P)
            elif sname == "body":
                tb = (" ".join(key_words(it["q"])), "*", None)
            else:
                tb = (it["q"], "*", None)
            # B 가 A 와 똑같으면 다시 찾지 않는다
            pb, sb = (pa, sa) if tb == (it["q"], "*", None) else at_search(*tb)
            m = mo.get((sname, it["id"]), (None, None))
            r = {"id": it["id"], "q": it["q"], "moai": m[0], "moai_s": m[1],
                 "ev_a": rank_paths(pa, it["answers"]), "ev_a_s": round(sa, 3),
                 "ev_b": rank_paths(pb, it["answers"]), "ev_b_s": round(sb, 3),
                 "ev_b_q": "{} | ext={} | dm={}".format(*tb)}
            rows.append(r)
            print("  {:<5} MoAI {:>3}  AtA {:>3}  AtB {:>3}  | {}".format(
                r["id"], r["moai"] or "-", r["ev_a"] or "-", r["ev_b"] or "-", r["ev_b_q"][:60]), flush=True)
        # summarize 는 moai_s 가 없으면 안 되므로 빈 값은 0 으로 채운다
        for r in rows:
            r["moai_s"] = r["moai_s"] or 0.0
        res[sname] = {"items": rows, "summary": summarize(rows)}
        print("   →", res[sname]["summary"], flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "anytxt_{}.json".format(time.strftime("%Y%m%d_%H%M")))
    json.dump(res, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("저장:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
