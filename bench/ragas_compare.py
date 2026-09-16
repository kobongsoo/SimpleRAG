#------------------------------------------------------------------
# 골든셋 RAGAS 두 결과 짝 비교 (계획서 0-1 · 이후 모든 실험)
#=> 같은 100문항을 두 번 채점한 결과(A, B)를 문항별로 짝지어 지표 평균 차이를 본다.
#
#   판정 잡음 폭
#    같은 답변을 두 번 채점한 A·B(0-1)의 차이는 순수한 판정 잡음이다. --save-noise 로 저장해 두면
#    이후 비교에서 "평균 차이가 잡음 폭(95%) 안인가" 를 자동으로 표시한다.
#      잡음 폭 = 1.96 × (문항별 차이의 표준편차) / √n  — 짝 비교 평균 차이의 95% 범위
#
#   사용:
#     python bench/ragas_compare.py baseline n01_rep2 --save-noise      # 잡음 폭 저장
#     python bench/ragas_compare.py baseline e11_dedup                   # 실험 비교(잡음 폭 자동 적용)
#   폴더 이름 baseline(또는 빈 문자열)은 results/ragas_golden 최상위(§36 기준선)를 뜻한다.
#------------------------------------------------------------------

import argparse
import collections
import io
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import ragas_golden as rg  # noqa: E402

NOISE_PATH = os.path.join(rg.OUT_DIR, "noise_band.json")


#------------------------------------------------------------------
# 실험 폴더의 문항별 점수 읽기
#=> baseline 은 최상위 폴더, 그 밖은 results/ragas_golden/<이름>/ 의 scores_items_<tag>.json 을 읽는다.
#    1) 정확히 그 태그 파일이 있으면 그것
#    2) 없으면 지표 부분집합 태그(scores_items_<tag>_f-ar-cr.json 등)가 하나뿐일 때 그것
#       — 기준선은 4지표 전체(nothink), 실험은 F·AR·CR 만(nothink_f-ar-cr) 채점하기 때문
#
# -in: name = 실험 ID 또는 "baseline"
# -in: tag  = 점수 파일 태그 (기본 nothink)
#
# -out: ({문항 id: 행 dict}, 읽은 파일 경로)
# -out: error = 후보가 없거나 여러 개면 SystemExit(경로를 알려 준다)
#------------------------------------------------------------------
def load_items(name, tag):
    import glob
    folder = rg.exp_dir("" if name in ("", "baseline") else name)
    path = os.path.join(folder, "scores_items_%s.json" % tag)
    if not os.path.exists(path):
        # limit 스모크 결과는 비교 대상에서 뺀다
        cands = [c for c in glob.glob(os.path.join(folder, "scores_items_%s_*.json" % tag)) if "_limit" not in c]
        if len(cands) != 1:
            raise SystemExit("점수 파일을 하나로 정할 수 없음: %s (후보 %d개)" % (path, len(cands)))
        path = cands[0]
    return {r["id"]: r for r in json.load(io.open(path, encoding="utf-8"))}, path


#------------------------------------------------------------------
# 한 지표의 짝 비교 통계
#=> 두 결과 모두 점수가 있는 문항만 짝지어 평균·차이·흔들림을 계산한다.
#    1) 문항별 차이 d = B − A
#    2) 평균 차이, 표준편차, 짝 비교 95% 범위(1.96·sd/√n)
#    3) 오른 문항·내린 문항 수, 0.5 이상 크게 바뀐 문항 수
#
# -in: A, B = {id: 행} 두 결과
# -in: col  = 지표 열 이름
# -in: ids  = 비교할 문항 id 목록 (None 이면 공통 전체)
#
# -out: dict (n, mean_a, mean_b, diff, sd, band95, up, down, big) 또는 짝이 없으면 None
# -out: error = 예외 없음
#------------------------------------------------------------------
def paired(A, B, col, ids=None):
    ids = ids if ids is not None else sorted(set(A) & set(B))
    pairs = [(A[i]["scores"].get(col), B[i]["scores"].get(col)) for i in ids if i in A and i in B]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(pairs)
    if not n:
        return None
    d = [b - a for a, b in pairs]
    mean_d = sum(d) / n
    sd = math.sqrt(sum((x - mean_d) ** 2 for x in d) / (n - 1)) if n > 1 else 0.0
    return {
        "n": n,
        "mean_a": round(sum(a for a, _ in pairs) / n, 4),
        "mean_b": round(sum(b for _, b in pairs) / n, 4),
        "diff": round(mean_d, 4),
        "sd": round(sd, 4),
        "band95": round(1.96 * sd / math.sqrt(n), 4),
        "up": sum(1 for x in d if x > 1e-9),
        "down": sum(1 for x in d if x < -1e-9),
        "big": sum(1 for x in d if abs(x) >= 0.5),
    }


#------------------------------------------------------------------
# 판정 문구
#=> 평균 차이를 저장된 잡음 폭과 견줘 사람이 읽을 판정을 붙인다.
#
# -in: diff  = 평균 차이 (B − A)
# -in: noise = 이 지표의 잡음 폭(95%) 또는 None
#
# -out: 문자열 ("잡음 이내" / "상승" / "하락" / "잡음 폭 없음")
# -out: error = 예외 없음
#------------------------------------------------------------------
def verdict(diff, noise):
    if noise is None:
        return "잡음 폭 없음"
    if abs(diff) <= noise:
        return "잡음 이내"
    return "상승" if diff > 0 else "하락"


#------------------------------------------------------------------
# 짝 비교 본체
#=> 두 실험의 점수를 읽어 지표별·유형별 차이 표를 찍고, 요청하면 잡음 폭을 저장한다.
#    1) 답변이 같은 문항 수를 먼저 센다 — 잡음 폭 저장은 전부 같을 때만 허용
#    2) 지표마다 짝 비교 통계를 내고, 저장된 잡음 폭이 있으면 판정을 붙인다
#    3) 질문 유형별 평균 차이를 따로 보인다
#
# -in: 없음 (sys.argv)
#
# -out: 0 = 성공
# -out: error = 점수 파일이 없거나 잡음 저장 조건이 안 맞으면 SystemExit
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="골든셋 RAGAS 두 결과 짝 비교")
    p.add_argument("a", help="기준 쪽 실험 ID (baseline = §36)")
    p.add_argument("b", help="비교 쪽 실험 ID")
    p.add_argument("--tag", default="nothink", help="점수 파일 태그 (scores_items_<tag>.json)")
    p.add_argument("--save-noise", action="store_true", help="A·B 가 같은 답변의 재채점일 때 잡음 폭으로 저장")
    args = p.parse_args()

    (A, path_a), (B, path_b) = load_items(args.a, args.tag), load_items(args.b, args.tag)
    # 같은 답변인지 확인 — 잡음 폭은 답변이 같을 때만 의미가 있다
    same_resp = sum(1 for i in A if i in B and A[i]["response"] == B[i]["response"])
    noise = json.load(io.open(NOISE_PATH, encoding="utf-8")) if os.path.exists(NOISE_PATH) and not args.save_noise else None

    print("A = %s  /  B = %s  /  답변 동일 %d/%d" % (args.a, args.b, same_resp, len(set(A) & set(B))))
    print("  A 파일 %s\n  B 파일 %s" % (os.path.relpath(path_a, rg.OUT_DIR), os.path.relpath(path_b, rg.OUT_DIR)))
    if noise:
        print("잡음 폭 기준: %s (%s)" % (noise["pair"], noise["created"]))

    print("\n| 지표 | n | A | B | B−A | 문항 차이 sd | 짝 95% 범위 | 오름/내림 | 차≥0.5 | 판정 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    stats = collections.OrderedDict()
    for key, (col, label) in rg.METRICS.items():
        s = paired(A, B, col)
        if not s:
            continue
        stats[col] = s
        band = noise["band95"].get(col) if noise else None
        print("| %s | %d | %.3f | %.3f | %+.3f | %.3f | ±%.3f | %d/%d | %d | %s |" % (
            label, s["n"], s["mean_a"], s["mean_b"], s["diff"], s["sd"], s["band95"],
            s["up"], s["down"], s["big"], "(잡음 측정)" if args.save_noise else verdict(s["diff"], band)))

    # 질문 유형별 차이 — 한 유형에서만 움직였는지 본다
    types = sorted({r["question_type"] for r in A.values()})
    print("\n| 유형 | " + " | ".join(label for _, label in rg.METRICS.values() if _ in stats) + " |")
    print("|---|" + "---:|" * len(stats))
    for t in types:
        ids = [i for i, r in A.items() if r["question_type"] == t]
        cells = []
        for col in stats:
            s = paired(A, B, col, ids)
            cells.append("-" if not s else "%+.3f (n=%d)" % (s["diff"], s["n"]))
        print("| %s | %s |" % (t, " | ".join(cells)))

    if args.save_noise:
        if same_resp != len(set(A) & set(B)):
            raise SystemExit("답변이 다른 문항이 있어 잡음 폭으로 저장하지 않음")
        import datetime
        rg.save_json(NOISE_PATH, {
            "pair": "%s vs %s" % (args.a, args.b), "tag": args.tag,
            "created": datetime.date.today().isoformat(),
            "band95": {col: s["band95"] for col, s in stats.items()},
            "item_sd": {col: s["sd"] for col, s in stats.items()},
            "mean_abs_diff_hint": {col: s["diff"] for col, s in stats.items()},
            "big_changes": {col: s["big"] for col, s in stats.items()},
        })
        print("\n잡음 폭 저장:", NOISE_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
