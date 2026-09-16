#------------------------------------------------------------------
# 두 평가 실행 비교 — McNemar 짝지은 검정
#=> 같은 문항을 두 버전으로 풀린 결과를 나란히 놓고 "정말 좋아졌는가"를
#   판정한다.
#
#   🔴 정답률 숫자만 비교하면 안 되는 이유
#      68% → 69% 는 문항 2개 차이다. 204문항에서 2개는 우연으로도 흔히
#      나온다. 중요한 것은 **총점이 아니라 어떤 문항이 뒤집혔는가**다.
#        b = A 만 맞음(잃은 문항)
#        c = B 만 맞음(얻은 문항)
#      총점이 같아도 b=20, c=20 이면 40문항이 뒤집힌 것이고, 그것은
#      "안 변했다"가 아니라 "완전히 다른 시스템이 됐다"는 뜻이다.
#
#   McNemar 는 b 와 c 만 본다. 둘 다 맞거나 둘 다 틀린 문항(a, d)은
#   두 버전을 구별하지 못하므로 정보가 없다.
#
#   사용:
#     python bench/compare_runs.py results/tablefix_answer.json \
#                                  results/appendix_answer.json
#     python bench/compare_runs.py A.json B.json --key hit3   # 검색 비교
#
#   성공 지표 필드 이름이 단계마다 다르다 — eval_large.py 가 그렇게 쓴다.
#     answer    단계 → correct   (retrieved_ok 도 있다)
#     retrieval 단계 → hit1 / hit3
#------------------------------------------------------------------

import argparse
import collections
import io
import json
import math
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")


#------------------------------------------------------------------
# 결과 파일 읽어 {질문: 성공여부} 로 만들기
#=> 검색 결과(hit1/hit3)와 답변 결과(ok)가 파일 형식이 달라서, 어느 열을
#   성공 지표로 쓸지 골라 받는다.
#
# -in: path = 평가 결과 JSON 경로(문항 목록)
# -in: key  = 성공 여부가 담긴 필드명 (correct / hit1 / hit3 / retrieved_ok)
#
# -out: (성적표 dict, 카테고리 dict) = {질문: bool}, {질문: 카테고리}
# -out: error = 파일이 없으면 FileNotFoundError, 필드가 없으면 KeyError
#------------------------------------------------------------------
def load(path, key):
    rows = json.load(io.open(path, encoding="utf-8"))
    if rows and key not in rows[0]:
        raise SystemExit("🔴 '%s' 에 '%s' 필드가 없다. 있는 필드: %s"
                         % (os.path.basename(path), key,
                            ", ".join(k for k in rows[0]
                                      if isinstance(rows[0][k], bool))))
    score, cat = {}, {}
    for r in rows:
        q = r["q"]
        score[q] = bool(r[key])
        cat[q] = r.get("cat", "?")
    return score, cat


#------------------------------------------------------------------
# McNemar 검정 (정확 이항 검정)
#=> 뒤집힌 문항 b+c 개 중 한쪽으로 몰린 정도가 동전 던지기로 설명되는지 본다.
#
#   카이제곱 근사 대신 **정확 이항 검정**을 쓴다. b+c 가 25 미만이면
#   근사가 부정확한데, 우리 실험은 대개 그 범위다.
#
# -in: b = A 만 맞은 문항 수
# -in: c = B 만 맞은 문항 수
#
# -out: p = 양측 p-value (b+c 가 0이면 1.0)
# -out: error = 없음
#------------------------------------------------------------------
def mcnemar_p(b, c):
    n = b + c
    if n == 0:
        return 1.0
    # 적은 쪽 이하가 나올 확률을 더하고, 대칭이므로 2배 한다.
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


#------------------------------------------------------------------
# 비교 결과 출력
#=> 총점 → 분할표 → 판정 → 카테고리별 → 뒤집힌 문항 목록 순으로 낸다.
#
# -in: a_score, b_score = {질문: bool} 두 개
# -in: cat              = {질문: 카테고리}
# -in: names            = (A 이름, B 이름)
# -in: show             = 뒤집힌 문항을 몇 개까지 나열할지
#
# -out: 없음 (표준출력)
# -out: error = 없음
#------------------------------------------------------------------
def report(a_score, b_score, cat, names, show=20):
    # 두 실행에 공통으로 있는 문항만 비교한다(문항 수가 다르면 짝이 안 맞는다).
    common = [q for q in a_score if q in b_score]
    n = len(common)
    only_a = len(a_score) - n
    only_b = len(b_score) - n
    if only_a or only_b:
        print("⚠️  한쪽에만 있는 문항 A %d개 / B %d개 — 제외했다" % (only_a, only_b))

    na = sum(a_score[q] for q in common)
    nb = sum(b_score[q] for q in common)

    print("=" * 66)
    print("  %-28s  %-28s" % (names[0], names[1]))
    print("  %3d/%d (%5.1f%%)%14s%3d/%d (%5.1f%%)"
          % (na, n, 100.0 * na / n, "", nb, n, 100.0 * nb / n))
    print("=" * 66)

    # 분할표
    both = sum(1 for q in common if a_score[q] and b_score[q])
    b_only = sum(1 for q in common if a_score[q] and not b_score[q])
    c_only = sum(1 for q in common if not a_score[q] and b_score[q])
    neither = n - both - b_only - c_only

    print()
    print("            B 맞음   B 틀림")
    print("  A 맞음    %5d    %5d" % (both, b_only))
    print("  A 틀림    %5d    %5d" % (c_only, neither))
    print()
    print("  잃음 b=%d   얻음 c=%d   뒤집힘 %d문항(%.0f%%)"
          % (b_only, c_only, b_only + c_only,
             100.0 * (b_only + c_only) / n))

    p = mcnemar_p(b_only, c_only)
    print("  McNemar p = %.3f  →  %s"
          % (p, "유의한 차이" if p < 0.05 else "유의차 없음"))

    # 🔴 뒤집힌 문항이 0개인 것과 몇 개뿐인 것은 뜻이 완전히 다르다.
    #    둘 다 p 는 크게 나오지만, 0개는 **모든 문항에서 결과가 같았다**는
    #    뜻이라 오히려 동등하다는 강한 증거다. 이것을 '검정력 없음'이라고
    #    적으면 회귀 확인 결과를 잘못 읽게 된다.
    if b_only + c_only == 0:
        print("  ✅ 완전 일치 — %d문항 전부에서 결과가 같다 (회귀 없음)" % n)
    elif b_only + c_only < 10:
        print("  ⚠️  뒤집힌 문항이 %d개뿐이라 검정력이 낮다 — "
              "'차이 없음'이 아니라 '알 수 없음'이다." % (b_only + c_only))

    # 카테고리별
    print("\n" + "-" * 66)
    print("카테고리별 (변화 있는 것만)")
    print("-" * 66)
    agg = collections.defaultdict(lambda: [0, 0, 0])   # [총, A맞음, B맞음]
    for q in common:
        s = agg[cat.get(q, "?")]
        s[0] += 1
        s[1] += a_score[q]
        s[2] += b_score[q]
    rows = [(k, v) for k, v in agg.items() if v[1] != v[2]]
    rows.sort(key=lambda kv: (kv[1][2] - kv[1][1]))
    if not rows:
        print("  (모든 카테고리 동점)")
    for k, (t, x, y) in rows:
        d = y - x
        print("  %-12s %2d문항  %3.0f%% → %3.0f%%   %+d"
              % (k[:12], t, 100.0 * x / t, 100.0 * y / t, d))

    # 뒤집힌 문항
    lost = [q for q in common if a_score[q] and not b_score[q]]
    won = [q for q in common if not a_score[q] and b_score[q]]
    for title, lst in (("🔴 잃은 문항", lost), ("🟢 얻은 문항", won)):
        print("\n%s %d건" % (title, len(lst)))
        for q in lst[:show]:
            print("   [%s] %s" % (cat.get(q, "?")[:8], q[:56]))
        if len(lst) > show:
            print("   … 외 %d건" % (len(lst) - show))


def main():
    ap = argparse.ArgumentParser(description="두 평가 실행 McNemar 비교")
    ap.add_argument("a", help="기준선 결과 JSON")
    ap.add_argument("b", help="비교 대상 결과 JSON")
    ap.add_argument("--key", default="correct",
                    help="성공 지표 필드 (correct=답변, hit1/hit3=검색)")
    ap.add_argument("--show", type=int, default=20, help="뒤집힌 문항 나열 수")
    args = ap.parse_args()

    a_score, cat_a = load(args.a, args.key)
    b_score, cat_b = load(args.b, args.key)
    cat_a.update(cat_b)

    names = (os.path.basename(args.a).replace(".json", ""),
             os.path.basename(args.b).replace(".json", ""))
    report(a_score, b_score, cat_a, names, args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
