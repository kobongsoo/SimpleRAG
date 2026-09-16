#------------------------------------------------------------------
# 506문항 핵심어 정답률 짝 비교 (McNemar) — 계획서 채택 조건 ②
#=> eval_large.py --stage answer 결과 두 벌(A 기준, B 실험)을 질문으로 짝지어
#   정답 수·잃음/얻음·McNemar 정확검정 p·TTFT 를 비교한다.
#
#   사용:
#     python bench/large_compare.py exp10_chunkfix/answer_v3_pool10.json results/ragas_golden/e11_dedup/large_answer_qwen3-0.6b-q4.json
#------------------------------------------------------------------

import argparse
import io
import json
import math
import statistics as st
import sys


#------------------------------------------------------------------
# 결과 파일 읽기
#=> 질문 문자열을 키로 쓴다(eval_large 결과에는 문항 id 가 없다).
#
# -in: path = eval_large 결과 JSON 경로
#
# -out: {질문: 행 dict}
# -out: error = 파일 없으면 FileNotFoundError, 같은 질문이 두 번이면 SystemExit
#------------------------------------------------------------------
def load(path):
    rows = json.load(io.open(path, encoding="utf-8"))
    out = {r["q"]: r for r in rows}
    if len(out) != len(rows):
        raise SystemExit("같은 질문이 여러 번 있음: " + path)
    return out


#------------------------------------------------------------------
# McNemar 정확검정 (양측)
#=> 한쪽만 맞힌 문항 수(잃음 b, 얻음 c)만으로 두 설정의 정답률 차이가 우연인지 본다.
#
# -in: b = A 만 정답(잃음), c = B 만 정답(얻음)
#
# -out: p 값 (불일치가 없으면 1.0)
# -out: error = 예외 없음
#------------------------------------------------------------------
def mcnemar_p(b, c):
    m = b + c
    if not m:
        return 1.0
    return min(1.0, 2 * sum(math.comb(m, i) for i in range(min(b, c) + 1)) / 2 ** m)


#------------------------------------------------------------------
# 분위수
#=> 정렬된 목록에서 비율 f 위치 값을 고른다(보간 없음 — REPORT 의 기존 표기와 같게).
#
# -in: vals = 숫자 목록, f = 0~1
#
# -out: 값
# -out: error = 빈 목록이면 IndexError
#------------------------------------------------------------------
def pct(vals, f):
    s = sorted(vals)
    return s[min(len(s) - 1, int(f * len(s)))]


#------------------------------------------------------------------
# 비교 본체
#=> 공통 질문만 짝지어 정답·검색 도달·TTFT 를 나란히 보이고, 바뀐 문항 목록을 찍는다.
#
# -in: 없음 (sys.argv)
#
# -out: 0 = 성공
# -out: error = 공통 질문이 없으면 SystemExit
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="506문항 정답률 짝 비교")
    p.add_argument("a", help="기준 결과 JSON")
    p.add_argument("b", help="실험 결과 JSON")
    p.add_argument("--show", type=int, default=30, help="바뀐 문항을 몇 개까지 찍을지")
    args = p.parse_args()

    A, B = load(args.a), load(args.b)
    qs = [q for q in A if q in B]
    if not qs:
        raise SystemExit("공통 질문 없음")
    lost = [q for q in qs if A[q]["correct"] and not B[q]["correct"]]
    gain = [q for q in qs if not A[q]["correct"] and B[q]["correct"]]
    ca, cb = sum(A[q]["correct"] for q in qs), sum(B[q]["correct"] for q in qs)
    n = len(qs)
    print("공통 문항 %d (A %d / B %d)" % (n, len(A), len(B)))
    print("| | A | B |\n|---|---:|---:|")
    print("| 정답 | %d (%.1f%%) | %d (%.1f%%) |" % (ca, 100 * ca / n, cb, 100 * cb / n))
    print("| 검색 도달 | %.1f%% | %.1f%% |" % (100 * sum(A[q]["retrieved_ok"] for q in qs) / n,
                                          100 * sum(B[q]["retrieved_ok"] for q in qs) / n))
    for label, key in (("TTFT", "ttft_s"), ("검색 ms", "retrieval_ms")):
        va, vb = [A[q][key] for q in qs], [B[q][key] for q in qs]
        print("| %s p50 / p90 | %.2f / %.2f | %.2f / %.2f |" % (
            label, st.median(va), pct(va, .9), st.median(vb), pct(vb, .9)))
    ta, tb = [A[q]["ttft_s"] for q in qs], [B[q]["ttft_s"] for q in qs]
    print("| 3초 이내 | %.1f%% | %.1f%% |" % (100 * sum(t <= 3 for t in ta) / n, 100 * sum(t <= 3 for t in tb) / n))
    src = sum(1 for q in qs if A[q].get("sources") != B[q].get("sources"))
    same = sum(1 for q in qs if A[q].get("answer") == B[q].get("answer"))
    print("\n잃음 %d / 얻음 %d / 순 %+d / McNemar p=%.3f / 근거 문서 달라짐 %d / 답변 동일 %d" % (
        len(lost), len(gain), len(gain) - len(lost), mcnemar_p(len(lost), len(gain)), src, same))
    for tag, lst in (("얻음", gain), ("잃음", lost)):
        for q in lst[:args.show]:
            print("  %s  [%s] %s" % (tag, A[q].get("cat", "-"), q[:60]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
