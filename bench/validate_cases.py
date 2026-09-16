#------------------------------------------------------------------
# 평가 문항 자기검증 — 정답이 실제로 문서에 있는가
#=> 사람이 쓴 문항은 반드시 틀린다. 채점 규칙 오류로 네 번 헛발질한
#   전례가 있다(설계서 R5). 그래서 **문항을 쓰고 나면 기계로 검증한다.**
#
#   검사 항목
#     ① src 조각이 실제 색인된 문서명과 맞는가 (오타·존재하지 않는 파일)
#     ② must 용어가 그 문서의 **색인된 텍스트**에 실제로 있는가
#        → 원문에 없는 답을 정답으로 요구하면 영원히 오답이 된다
#     ③ must 용어가 지나치게 흔하지 않은가
#        → 코퍼스 절반에 나오는 표현이면 검색 없이도 맞출 수 있다
#     ④ 질문이 중복되지 않는가
#
#   ⚠️ 텍스트 캐시(build_textcache.py)가 있어야 한다. 캐시는 인덱싱과 같은
#      추출기로 만들었으므로 '색인된 텍스트'와 동일하다.
#
#   사용:
#     python bench/validate_cases.py
#     python bench/validate_cases.py --cases eval_cases_v2
#------------------------------------------------------------------

import argparse
import collections
import importlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _HERE)

CACHE = os.path.join(ROOT, "results", "textcache")
MANIFEST = os.path.join(CACHE, "manifest.json")

flat = lambda s: re.sub(r"\s+", "", s)


#------------------------------------------------------------------
# must 표기 정규화
#=> "a|b" 는 둘 중 하나면 인정. 검증도 같은 규칙으로 본다.
#------------------------------------------------------------------
def alts(spec):
    return [flat(a) for a in spec.split("|")]


def main():
    ap = argparse.ArgumentParser(description="평가 문항 자기검증")
    ap.add_argument("--cases", default="eval_cases_v2")
    ap.add_argument("--common-ratio", type=float, default=0.30,
                    help="must 용어가 이 비율 넘는 문서에 나오면 '너무 흔함'")
    args = ap.parse_args()

    mod = importlib.import_module(args.cases)
    cases = mod.CASES

    man = json.load(open(MANIFEST, encoding="utf-8"))
    docs = {}          # 파일명 -> 평탄화 본문
    for p, v in man.items():
        fp = os.path.join(CACHE, v["cache"])
        if os.path.isfile(fp):
            docs[os.path.basename(p)] = flat(open(fp, encoding="utf-8").read())
    print("문항 %d개 / 캐시 문서 %d건\n" % (len(cases), len(docs)))

    bad_src, bad_must, too_common, dup = [], [], [], []
    seen_q = collections.Counter()

    for c in cases:
        seen_q[c["q"]] += 1

        # ① src 가 실제 문서에 걸리는가
        hits = [n for n in docs if any(f in n for f in c["src"])]
        if not hits:
            bad_src.append((c["q"], c["src"]))
            continue

        # ② must 가 그 문서들 중 하나에 실제로 있는가
        #    표 추론 문항은 정답 문자열이 원문에 그대로 없는 것이 정상이다
        #    (예: 표에 "5" 와 머리글 "휴가(일)" 만 있고 "5일" 은 없다).
        if c.get("kind") != "표추론":
            for spec in c["must"]:
                ok = any(any(a in docs[n] for a in alts(spec)) for n in hits)
                if not ok:
                    bad_must.append((c["q"], spec, hits[0]))

        # ③ 너무 흔한 용어인가 (검색 없이도 맞출 수 있는 문항)
        for spec in c["must"]:
            n_docs = sum(1 for n, t in docs.items()
                         if any(a in t for a in alts(spec)))
            if n_docs > len(docs) * args.common_ratio:
                too_common.append((c["q"], spec, n_docs))

    dup = [q for q, n in seen_q.items() if n > 1]

    def show(title, rows, fmt):
        print("%s %d건" % (title, len(rows)))
        for r in rows[:40]:
            print("   " + fmt(r))
        if len(rows) > 40:
            print("   … 외 %d건" % (len(rows) - 40))
        print()

    show("🔴 src 가 어떤 문서에도 안 걸림", bad_src,
         lambda r: "%-52s src=%s" % (r[0][:52], r[1]))
    show("🔴 must 가 원문에 없음 (영원히 오답이 되는 문항)", bad_must,
         lambda r: "%-46s must=%-16s doc=%s" % (r[0][:46], r[1][:16], r[2][:28]))
    show("🟡 must 가 너무 흔함 (검색 없이도 맞출 수 있음)", too_common,
         lambda r: "%-46s must=%-14s %d개 문서" % (r[0][:46], r[1][:14], r[2]))
    show("🟡 질문 중복", dup, lambda r: r[:70])

    total_bad = len(bad_src) + len(bad_must)
    print("=" * 60)
    print("치명 %d건 / 경고 %d건" % (total_bad, len(too_common) + len(dup)))
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
