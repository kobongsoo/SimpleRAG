#------------------------------------------------------------------
# 메타데이터 질의 해석 시험 (메타데이터 검색 설계서 §16-1 ⑤ ⑥ · 4단계)
#=> ⑤ 본문 질의 오탐 — 골든셋 본문 질문(조건이 없어야 하는 질문)에 메타 조건이 붙는지 센다(합격: 2문항 이하)
#   ⑥ 시간 — 해석 + 필터 계획이 질의당 몇 ms 인지(합격: +10ms 이내)
#   메타 질의 예시 — 설계서의 풀고 싶은 질의가 의도대로 나뉘는지 본다
#   meta.sqlite 가 없으면 ⑤는 이름 목록 없이(사람 판정 없이) 돈다.
#
#   사용: .venv\Scripts\python.exe bench\test_meta_query.py [--db <meta.sqlite>]
#------------------------------------------------------------------

import argparse
import glob
import json
import os
import sys
import time
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from simplerag.meta import query as Q  # noqa: E402
from simplerag.meta.rules import load_rules  # noqa: E402

TODAY = date(2026, 10, 29)


#------------------------------------------------------------------
# 골든셋 본문 질문 모으기
#=> dataset/ 의 골든 · 바꿔 말한 질문 jsonl 에서 user_input(RAGAS 모양) 칸을 모은다.
#
# -in: 없음
#
# -out: [질문, …] (중복 없음)
# -out: error = 없음 (못 읽는 파일은 건너뜀)
#------------------------------------------------------------------
def golden_questions():
    out = []
    for p in sorted(glob.glob(os.path.join(_ROOT, "dataset", "*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                # 골든셋은 RAGAS 모양(user_input)이다
                q = d.get("user_input") or d.get("question") or d.get("q") or d.get("query")
                if isinstance(q, str) and q.strip():
                    out.append(q.strip())
    return list(dict.fromkeys(out))


#------------------------------------------------------------------
# 실행 — ⑤ 오탐 · ⑥ 시간 · 예시 질의
#
# -in: 없음 (명령행 --db)
#
# -out: 0 = 합격, 1 = 불합격
# -out: error = 없음
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    db = args.db
    root = os.path.dirname(os.path.abspath(db)) if db else None
    names, aliases, me = Q._names(db, root) if db else (set(), {}, None)
    rules = load_rules(root)

    qs = golden_questions()
    hits = []
    t0 = time.perf_counter()
    for q in qs:
        p = Q.parse(q, names, aliases, me, rules, TODAY)
        if p["conditions"]:
            hits.append((q, p))
    parse_ms = (time.perf_counter() - t0) * 1000 / max(1, len(qs))
    print("⑤ 본문 질문 {}개 중 조건이 붙은 질문 {}개 (합격 ≤ 2)".format(len(qs), len(hits)))
    for q, p in hits[:20]:
        what = [k for k in ("people", "date", "format", "recv", "scanned") if p[k]]
        print("   - {} → {} {}".format(q[:60], what, [x["name"] for x in p["people"]] or (p["date"] or {}).get("label", "")))
    print("⑥ 해석만: 질의당 {:.2f}ms".format(parse_ms))

    if db:
        examples = ["작년에 고봉수님이 만든 절차서 양식을 찾아줘", "메일로 받은 한글 파일 중 점검 확인서", "지난주에 받은 제안서",
                    "스캔된 계약서", "2023년 정기점검 보고서", "휴가 규정에서 연차 이월", "내가 만든 엑셀", "조강래가 작성한 매뉴얼"]
        Q.plan(examples[0], db=db, today=TODAY, root=root)     # 첫 읽기(캐시)는 따로
        t0 = time.perf_counter()
        for q in examples:
            r = Q.plan(q, db=db, today=TODAY, root=root)
            print("   {} → {}".format(q, "조건 없음" if r is None else "본문 {!r} · 문서 {} · {} {}".format(
                r["query"], "전체" if r["docs"] is None else len(r["docs"]),
                [(c["label"], c["mode"]) for c in r["chips"]], r["relaxed"] or "")))
        print("⑥ 해석 + 필터 계획: 질의당 {:.2f}ms (합격 ≤ 10)".format((time.perf_counter() - t0) * 1000 / len(examples)))
    return 0 if len(hits) <= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
