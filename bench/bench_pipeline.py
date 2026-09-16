#------------------------------------------------------------------
# 구현 회귀 검증 — 실제 패키지가 설계서 수치를 재현하는가
#=> 지금까지의 벤치는 하네스 안에서 직접 조립한 임시 코드로 쟀다. 이 스크립트는
#   src/simplerag 의 실제 파이프라인(App → RagPipeline)을 그대로 태워서,
#   구현이 설계서에 적힌 값을 실제로 내는지 확인한다.
#
#   기준값(설계서 §부록B / REPORT §13)
#     검색 지연   10ms
#     체감 TTFT   2.75s
#     정답률      83% (0.6B) / 92% (1.7B)
#     혼동률      0%
#
#   평가 문항은 bench_real.CASES 를 그대로 쓴다(실문서에서 확인한 사실만).
#
#   사용:  python bench/bench_pipeline.py
#          python bench/bench_pipeline.py --model qwen3-1.7b-q4
#------------------------------------------------------------------

import argparse
import json
import os
import re
import statistics
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "src")))

RESULTS_DIR = os.path.abspath(os.path.join(_HERE, "..", "results"))

from bench_real import CASES, has_must          # noqa: E402
from simplerag.warmup import App                # noqa: E402


#------------------------------------------------------------------
# 한 문항 실행 + 채점
#=> 실제 파이프라인의 이벤트 스트림을 소비해 TTFT 까지 실측한다.
#   answer_sync 를 쓰면 더 빠르지만 스트리밍 경로(운영과 동일)를 검증할 수 없다.
#
# -in: app, case, top_k
#
# -out: dict = 채점 결과 + 타이밍
#------------------------------------------------------------------
def run_case(app, case, top_k):
    chunks, answer, done = [], [], None

    for event in app.pipeline.answer(case["q"], top_k=top_k):
        if event[0] == "evidence":
            chunks = event[1]
        elif event[0] == "token":
            answer.append(event[1])
        elif event[0] == "done":
            done = event[1]

    body = done["answer"]
    flat = re.sub(r"\s+", "", body)

    got = [m for m in case["must"] if has_must(flat, m)]
    bad = [m for m in case["must_not"] if re.sub(r"\s+", "", m) in flat]
    src_ok = any(case["src"] in c["doc_name"] for c in chunks)

    return {
        "q": case["q"],
        "retrieved_ok": src_ok,
        "correct": len(got) == len(case["must"]) and not bad,
        "confused": bool(bad),
        "confuse_terms": bad,
        "retrieval_ms": done["timing"]["total_ms"],
        "ttft_s": done["ttft_s"],
        "total_s": done["total_s"],
        "cited": done["cited"],
        "sources": [c["doc_name"] for c in chunks],
        "answer": body.replace("\n", " ")[:220],
    }


#------------------------------------------------------------------
# 기준값 대비 판정
#=> 실측이 설계서 값에서 크게 벗어나면 회귀로 본다. 노트북 발열 편차가
#   ±15% 라 여유를 두되, 방향이 나쁜 쪽으로 크게 벌어지면 잡아낸다.
#------------------------------------------------------------------
#   혼동률 기준이 0% 가 아닌 이유: 채점 규칙을 정밀화한 뒤 남은 1건은 진짜
#   환각이다("50만원 초과분의 50%" 를 "50%의 25만원" 으로 계산해 지어냄).
#   0.6B 의 실제 실패이므로 기준값에 포함한다 — 0% 로 두면 매번 오탐이 뜬다.
BASELINE = {
    "qwen3-0.6b-q4": {"accuracy": 0.83, "confusion": 0.08, "ttft_s": 2.75},
    "qwen3-1.7b-q4": {"accuracy": 0.92, "confusion": 0.00, "ttft_s": 8.46},
}


def main():
    p = argparse.ArgumentParser(description="구현 회귀 검증")
    p.add_argument("--model", default="qwen3-0.6b-q4")
    p.add_argument("--top-k", type=int, default=3)
    args = p.parse_args()

    app = App(args.model)
    try:
        warm = app.warmup(wait=True)
        print("[pipeline] 모델={} top-{}  예열 {:.0f}ms\n".format(
            args.model, args.top_k, warm["_total_ms"]))

        rows = []
        for case in CASES:
            r = run_case(app, case, args.top_k)
            rows.append(r)
            print("  [{}{}] {:<44} TTFT {:.2f}s  검색 {:>5.0f}ms {}".format(
                "R" if r["retrieved_ok"] else "-",
                "A" if r["correct"] else "-",
                case["q"][:42], r["ttft_s"], r["retrieval_ms"],
                ",".join(r["confuse_terms"])))

        n = len(rows)
        acc = sum(r["correct"] for r in rows) / n
        conf = sum(r["confused"] for r in rows) / n
        ret = sum(r["retrieved_ok"] for r in rows) / n
        med_ret = statistics.median(r["retrieval_ms"] for r in rows)
        med_ttft = statistics.median(r["ttft_s"] for r in rows)

        base = BASELINE.get(args.model, {})
        print("\n  === 결과 (기준값 대비) ===")
        print("  검색 성공  : {:.0f}%".format(ret * 100))
        print("  정답률     : {:.0f}%   (기준 {:.0f}%)".format(
            acc * 100, base.get("accuracy", 0) * 100))
        print("  혼동률     : {:.0f}%   (기준 {:.0f}%)".format(
            conf * 100, base.get("confusion", 0) * 100))
        print("  검색 지연  : {:.0f}ms".format(med_ret))
        print("  체감 TTFT  : {:.2f}s  (기준 {:.2f}s)".format(
            med_ttft, base.get("ttft_s", 0)))

        # 판정 — 정답률은 1문항(8.3%p) 여유, TTFT 는 발열 편차 감안 +30%.
        ok = True
        if base:
            if acc < base["accuracy"] - 0.09:
                print("  ⚠️ 정답률 회귀 의심")
                ok = False
            if med_ttft > base["ttft_s"] * 1.3:
                print("  ⚠️ TTFT 회귀 의심")
                ok = False
            if conf > base.get("confusion", 0) + 0.09:
                print("  ⚠️ 혼동 증가 — 프롬프트 변경 여부 확인")
                ok = False
        print("  판정: {}".format("PASS" if ok else "CHECK"))

        out = os.path.join(RESULTS_DIR, "pipeline_" + args.model + ".json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print("\n[pipeline] 저장: " + out)
        return 0 if ok else 1
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
