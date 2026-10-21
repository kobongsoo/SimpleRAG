#------------------------------------------------------------------
# 골든셋 v2 — 두 실험(예: legacy vs blocks 추출 엔진) 포맷별 비교
#=> ragas_golden.py 로 모은 두 실험 폴더의 답변·점수를 문항별로 짝지어, 포맷(doc·pdf·hwp …)과
#   근거 종류(본문·표·슬라이드 …)별로 나눠 본다. RAGAS 판정 없이도 볼 수 있는 규칙 지표를 함께 낸다.
#
#   규칙 지표 (판정 LLM 불필요)
#    · 문서 적중  — sLLM 에 들어간 근거 3건 중 정답 문서가 있는가 (responses.doc_hit)
#    · 근거 적중  — 정답 발췌문(reference_contexts)이 근거 청크 안에 실제로 들어 있는 비율
#                   (공백·'|' 를 지우고 비교 — 두 엔진의 표 렌더링 차이를 없앤다)
#    · 핵심어 정답 — 답변에 must 핵심어가 모두 들어 있는가
#   RAGAS 지표 — scores_items_*.json 이 있으면 Faithfulness·AR·CP·CR 평균
#
#   짝 비교: 이진 지표는 A만 맞힘 / B만 맞힘 개수와 McNemar 정확검정 p 값.
#
#   사용: python bench/golden_v2_compare.py g2_legacy g2_blocks
#------------------------------------------------------------------

import argparse
import collections
import glob
import io
import json
import math
import os
import re
import sys
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(_HERE), "results", "ragas_golden")
RAGAS_COLS = [("faithfulness", "F"), ("answer_relevancy", "AR"),
              ("llm_context_precision_with_reference", "CP"), ("context_recall", "CR")]


#------------------------------------------------------------------
# 비교용 정규화 — NFKC + 공백·표 구분자 제거
#
# -in: s = 글
#
# -out: str
# -out: error = 없음
#------------------------------------------------------------------
def norm(s):
    return re.sub(r"[\s|｜]+", "", unicodedata.normalize("NFKC", s or ""))


#------------------------------------------------------------------
# 발췌문 하나가 근거 안에 있는가
#=> 발췌문이 청크 경계에 걸려 둘로 나뉠 수 있어, 통째로 없으면 앞·가운데·끝 40자 조각 중
#   하나라도 있으면 '들어 있음'으로 본다.
#
# -in: ctx  = 정규화한 발췌문
# -in: pool = 정규화한 근거 청크들을 이은 글
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def ctx_found(ctx, pool):
    if ctx in pool:
        return True
    if len(ctx) <= 40:
        return False
    starts = (0, len(ctx) // 2 - 20, len(ctx) - 40)
    return any(ctx[i:i + 40] in pool for i in starts)


#------------------------------------------------------------------
# 핵심어 판정 — "a|b" 는 택일
#
# -in: answer = 정규화한 답변
# -in: musts  = 핵심어 목록
#
# -out: bool = 모두 들어 있으면 True
# -out: error = 없음
#------------------------------------------------------------------
def must_ok(answer, musts):
    return all(any(norm(alt) in answer for alt in m.split("|")) for m in musts)


#------------------------------------------------------------------
# 실험 폴더 읽기 — 답변 + (있으면) RAGAS 점수
#=> scores_items_*.json 이 여러 개(지표 부분집합)면 문항 id 로 합친다.
#
# -in: name = 실험 ID(results/ragas_golden/<ID>)
#
# -out: {id: 문항 dict(규칙 지표 · scores 포함)}
# -out: error = responses.json 없으면 FileNotFoundError
#------------------------------------------------------------------
def load(name):
    d = os.path.join(OUT_DIR, name)
    rows = json.load(io.open(os.path.join(d, "responses.json"), encoding="utf-8"))
    scores = collections.defaultdict(dict)
    for p in sorted(glob.glob(os.path.join(d, "scores_items_*.json"))):
        if "limit" in os.path.basename(p):
            continue
        for it in json.load(io.open(p, encoding="utf-8")):
            for k, v in (it.get("scores") or {}).items():
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    scores[it["id"]][k] = v
    out = {}
    for r in rows:
        pool = norm("".join(r.get("retrieved_contexts", [])))
        ctxs = [norm(c) for c in r.get("reference_contexts", [])]
        found = sum(ctx_found(c, pool) for c in ctxs)
        out[r["id"]] = dict(r, doc_ok=bool(r.get("doc_hit")),
                            ctx_rate=found / len(ctxs) if ctxs else 0.0,
                            ctx_ok=bool(ctxs) and found == len(ctxs),
                            must_ok=must_ok(norm(r.get("response", "")), r.get("must") or []),
                            scores=scores.get(r["id"], {}))
    return out


#------------------------------------------------------------------
# McNemar 정확검정(양측) p 값
#
# -in: b = A만 맞힌 수
# -in: c = B만 맞힌 수
#
# -out: float p 값 (불일치가 없으면 1.0)
# -out: error = 없음
#------------------------------------------------------------------
def mcnemar(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


#------------------------------------------------------------------
# 한 묶음(전체·포맷·근거 종류)의 비교 요약
#
# -in: ids = 문항 id 목록
# -in: A, B = load() 결과
#
# -out: dict = 지표별 A·B 값과 짝 비교
# -out: error = 없음
#------------------------------------------------------------------
def summarize(ids, A, B):
    s = {"n": len(ids)}
    for key in ("doc_ok", "ctx_ok", "must_ok"):
        a = sum(A[i][key] for i in ids)
        b = sum(B[i][key] for i in ids)
        only_a = sum(A[i][key] and not B[i][key] for i in ids)
        only_b = sum(B[i][key] and not A[i][key] for i in ids)
        s[key] = {"A": a, "B": b, "only_A": only_a, "only_B": only_b, "p": round(mcnemar(only_a, only_b), 4)}
    s["ctx_rate"] = {"A": round(sum(A[i]["ctx_rate"] for i in ids) / max(1, len(ids)), 3),
                     "B": round(sum(B[i]["ctx_rate"] for i in ids) / max(1, len(ids)), 3)}
    for col, label in RAGAS_COLS:
        pairs = [(A[i]["scores"].get(col), B[i]["scores"].get(col)) for i in ids]
        pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
        if pairs:
            s[label] = {"A": round(sum(x for x, _ in pairs) / len(pairs), 3),
                        "B": round(sum(y for _, y in pairs) / len(pairs), 3), "n": len(pairs)}
    return s


#------------------------------------------------------------------
# 요약 한 줄 출력
#
# -in: name = 묶음 이름
# -in: s    = summarize 결과
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def show(name, s):
    def pair(k):
        v = s[k]
        return "{:>3}→{:<3}(+{}/-{} p{:.2f})".format(v["A"], v["B"], v["only_B"], v["only_A"], v["p"])
    rag = " ".join("{} {:.2f}→{:.2f}".format(lab, s[lab]["A"], s[lab]["B"])
                   for _, lab in RAGAS_COLS if lab in s)
    print("{:<14} n={:<3} 문서 {} 근거 {} 핵심어 {} {}".format(
        name, s["n"], pair("doc_ok"), pair("ctx_ok"), pair("must_ok"), rag))


#------------------------------------------------------------------
# 진입점
#
# -in: 없음 (sys.argv: A B [--out 경로])
#
# -out: int 종료 코드
# -out: error = 폴더가 없으면 FileNotFoundError
#------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="골든셋 v2 두 실험 포맷별 비교")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--out", default="", help="요약 JSON 저장 경로(생략 시 results/ragas_golden/<a>_vs_<b>.json)")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    A, B = load(args.a), load(args.b)
    ids = [i for i in A if i in B]
    groups = collections.OrderedDict([("전체", ids)])
    for key in ("format", "evidence_kind", "question_type"):
        vals = sorted({A[i].get(key) for i in ids if A[i].get(key)})
        for v in vals:
            groups["{}:{}".format(key, v)] = [i for i in ids if A[i].get(key) == v]
    res = collections.OrderedDict((g, summarize(v, A, B)) for g, v in groups.items())
    print("A = {} / B = {}   (값은 A→B, +B만 맞힘/-A만 맞힘)".format(args.a, args.b))
    for g, s in res.items():
        show(g, s)
    # 문항별 차이 목록 — 보고서에서 사례를 뽑는다
    diffs = [{"id": i, "format": A[i].get("format"), "q": A[i]["user_input"],
              "A": {k: A[i][k] for k in ("doc_ok", "ctx_ok", "must_ok")},
              "B": {k: B[i][k] for k in ("doc_ok", "ctx_ok", "must_ok")},
              "A_docs": A[i].get("retrieved_docs"), "B_docs": B[i].get("retrieved_docs")}
             for i in ids if any(A[i][k] != B[i][k] for k in ("doc_ok", "ctx_ok", "must_ok"))]
    out = args.out or os.path.join(OUT_DIR, "{}_vs_{}.json".format(args.a, args.b))
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"a": args.a, "b": args.b, "groups": res, "diffs": diffs}, f, ensure_ascii=False, indent=1)
    print("\n저장:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
