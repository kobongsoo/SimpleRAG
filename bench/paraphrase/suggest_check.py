"""동의어 후보 제안 품질 — 바꿔 말한 질문 세트의 바꾼 짝(swaps)이 후보 1위·상위 3 안에 드는지.

사전 없이(SIMPLERAG_SYNONYMS=0) 세트 전체를 놓친 질문으로 넣어 만든 후보 파일을 채점한다.
사용:
  SIMPLERAG_SYNONYMS=0 simplerag suggest-synonyms --from dataset/paraphrase_pdf30.jsonl --out <후보.json>
  python bench/paraphrase/suggest_check.py <후보.json> [<다른 후보.json>]   (두 개면 두 판 비교)
"""
import io
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SET = os.path.join(ROOT, "dataset", "paraphrase_pdf30.jsonl")


#------------------------------------------------------------------
# 같은 용어로 보기
#=> 공백을 지우고 한쪽이 다른 쪽으로 시작하면 같다고 본다("배우자의" ↔ "배우자", "사망 시" ↔ "사망").
#
# -in: gold = 정답 문서 용어 / term = 후보 용어
#
# -out: True = 같은 용어
# -out: error = 없음
#------------------------------------------------------------------
def same(gold, term):
    g, t = re.sub(r"\s+", "", gold), re.sub(r"\s+", "", term)
    return bool(t) and (g.startswith(t) or t.startswith(g))


#------------------------------------------------------------------
# 후보 파일 채점
#=> 정답 짝(일상어 한 낱말 → 문서 용어)마다, 같은 낱말(앞 두 글자)로 시작하는 후보 낱말의 1위·상위 3 을 본다.
#   후보 낱말로 뽑히지 않은 짝은 놓침으로 센다.
#
# -in: path = 후보 json
#
# -out: (1위 적중, 상위 3 적중, 정답 짝 수, 짝별 결과 dict)
# -out: error = 파일이 없으면 예외 전파
#------------------------------------------------------------------
def score(path):
    cand = json.load(io.open(path, encoding="utf-8"))["candidates"]
    pairs = []
    for line in io.open(SET, encoding="utf-8"):
        r = json.loads(line)
        for cw, dt in r["swaps"]:
            if " " not in cw:
                pairs.append((cw, dt))
    pairs = list(dict.fromkeys(pairs))
    top1 = top3 = 0
    detail = {}
    for cw, dt in pairs:
        ent = next((c for c in cand if c["word"].startswith(cw[:2]) and cw.startswith(c["word"][:2])), None)
        terms = [t["term"] for t in ent["terms"]] if ent else []
        h1 = bool(terms) and same(dt, terms[0])
        h3 = any(same(dt, t) for t in terms[:3])
        top1 += h1
        top3 += h3
        detail[(cw, dt)] = (h1, h3, ent["word"] if ent else None, terms)
    return top1, top3, len(pairs), detail


#------------------------------------------------------------------
# 진입점 — 후보 파일 하나면 채점, 둘이면 두 판 후보가 같은지도 본다
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    results = [score(p) for p in sys.argv[1:]]
    for p, (t1, t3, n, detail) in zip(sys.argv[1:], results):
        for (cw, dt), (h1, h3, w, terms) in detail.items():
            print("{} {} → {}  | 후보 낱말 {} : {}".format("✅" if h1 else ("◐" if h3 else "  "), cw, dt, w, terms))
        print("{} — 정답 짝 {}개: 1위 {} · 상위 3 {}".format(os.path.basename(p), n, t1, t3))
    if len(results) == 2:
        a, b = results[0][3], results[1][3]
        diff = [k for k in a if a[k][3] != b[k][3]]
        print("두 파일의 후보가 다른 짝 {}개: {}".format(len(diff), diff))


if __name__ == "__main__":
    main()
