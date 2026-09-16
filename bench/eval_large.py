#------------------------------------------------------------------
# 확대 평가 하네스 (D:\분류함 전체 375건 대상)
#=> 기존 평가는 회사규정 18건 / 12문항이었다. 문서가 20배 늘면 방해 문서가
#   크게 늘어 검색 난이도가 완전히 달라지므로 전체 인덱스에서 다시 잰다.
#
#   기존 평가와 달라진 점
#    1) 정답 출처를 '복수'로 인정한다 — #외부유출금지_회사규정# 폴더에
#       2.회사규정 과 같은 문서가 중복 존재한다. 어느 쪽이 와도 정답이다.
#    2) Recall@1 / Recall@3 을 함께 본다. 문서가 많아지면 @1 이 먼저 무너진다.
#    3) 문항을 카테고리로 나눠, 어떤 종류의 질문에서 무너지는지 본다.
#
#   사용:
#     python bench/eval_large.py --stage retrieval          # 검색만(빠름)
#     python bench/eval_large.py --stage answer --model qwen3-0.6b-q4
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

# 평가셋은 실행 시점에 고른다(--cases). 기본은 v1 45문항 — 기존 측정과의
# 비교 가능성을 지키기 위해서다. 확대 평가는 --cases eval_cases_v2.
CASES = []                                       # main() 에서 채운다
from simplerag.warmup import App                  # noqa: E402
from simplerag import config                      # noqa: E402


#------------------------------------------------------------------
# must 키워드 판정 ("a|b" 는 택일)
#=> 같은 사실의 표기 방식이 여러 가지인 경우(2백만원/200만원)를 인정한다.
#------------------------------------------------------------------
def has_must(flat, spec):
    return any(re.sub(r"\s+", "", alt) in flat for alt in spec.split("|"))


#------------------------------------------------------------------
# 출처 일치 판정 (복수 정답 허용)
#=> case["src"] 는 파일명 조각 목록이다. 중복 문서(같은 규정이 두 폴더에)나
#   같은 사실이 여러 문서에 있는 경우를 모두 정답으로 본다.
#
# -in: chunks = 검색 결과, case
#
# -out: bool
#------------------------------------------------------------------
def src_hit(chunks, case):
    names = [c.get("doc_name", "") for c in chunks]
    return any(frag in n for frag in case["src"] for n in names)


#------------------------------------------------------------------
# 검색만 평가 (LLM 미사용 — 빠르다)
#=> Recall@1 / @3 을 문항 카테고리별로 집계한다.
#------------------------------------------------------------------
def stage_retrieval(app, args):
    rows = []
    for case in CASES:
        t0 = time.perf_counter()
        top3, timing = app.retriever.search(case["q"], top_k=3)
        ms = (time.perf_counter() - t0) * 1000

        rows.append({
            "q": case["q"], "cat": case.get("cat", "-"),
            "hit1": src_hit(top3[:1], case),
            "hit3": src_hit(top3, case),
            "ms": round(ms, 1),
            "sources": [c["doc_name"] for c in top3],
            "folders": [c.get("folder", "") for c in top3],
        })
        print("  [{}{}] {:<52} {:>6.0f}ms  {}".format(
            "1" if rows[-1]["hit1"] else "-",
            "3" if rows[-1]["hit3"] else "-",
            case["q"][:50], ms, rows[-1]["sources"][0][:34]))

    _summarize_retrieval(rows)
    return rows


#------------------------------------------------------------------
# 검색 결과 요약 (전체 + 카테고리별)
#------------------------------------------------------------------
def _summarize_retrieval(rows):
    n = len(rows)
    r1 = sum(r["hit1"] for r in rows) / n
    r3 = sum(r["hit3"] for r in rows) / n
    print("\n  === 검색 (n={}) ===".format(n))
    print("  Recall@1 : {:.0f}%".format(r1 * 100))
    print("  Recall@3 : {:.0f}%".format(r3 * 100))
    print("  지연     : 중앙값 {:.0f}ms / 최대 {:.0f}ms".format(
        statistics.median(r["ms"] for r in rows), max(r["ms"] for r in rows)))

    cats = {}
    for r in rows:
        cats.setdefault(r["cat"], []).append(r)
    print("\n  카테고리별 Recall@3:")
    for cat, rs in sorted(cats.items()):
        print("    {:<14} {:>3}문항  {:>4.0f}%".format(
            cat, len(rs), sum(x["hit3"] for x in rs) / len(rs) * 100))


#------------------------------------------------------------------
# 답변까지 평가 (검색 + 생성)
#=> 실제 파이프라인을 그대로 태운다. 검색 실패와 생성 실패를 분리해 집계해야
#   어디를 고쳐야 하는지 알 수 있다.
#------------------------------------------------------------------
def stage_answer(app, args):
    rows = []
    for i, case in enumerate(CASES, 1):
        chunks, done = [], None
        for event in app.pipeline.answer(case["q"], top_k=args.top_k,
                                         max_tokens=args.max_tokens):
            if event[0] == "evidence":
                chunks = event[1]
            elif event[0] == "done":
                done = event[1]

        body = done["answer"]
        flat = re.sub(r"\s+", "", body)
        got = [m for m in case["must"] if has_must(flat, m)]
        bad = [m for m in case.get("must_not", []) if re.sub(r"\s+", "", m) in flat]
        hit3 = src_hit(chunks, case)

        rows.append({
            "q": case["q"], "cat": case.get("cat", "-"),
            "retrieved_ok": hit3,
            "correct": len(got) == len(case["must"]) and not bad,
            "confused": bool(bad), "confuse_terms": bad,
            "retrieval_ms": done["timing"]["total_ms"],
            "ttft_s": done["ttft_s"], "total_s": done["total_s"],
            "sources": [c["doc_name"] for c in chunks],
            "answer": body.replace("\n", " ")[:240],
        })
        print("  [{:>2}] [{}{}] {:<46} TTFT {:.2f}s {}".format(
            i, "R" if hit3 else "-", "A" if rows[-1]["correct"] else "-",
            case["q"][:44], rows[-1]["ttft_s"],
            ",".join(bad)))

    _summarize_answer(rows, args)
    return rows


#------------------------------------------------------------------
# 답변 결과 요약
#=> 핵심은 '검색은 됐는데 생성이 틀린' 비율이다. 이 값이 모델 교체의 근거가 된다.
#------------------------------------------------------------------
def _summarize_answer(rows, args):
    n = len(rows)
    ret = sum(r["retrieved_ok"] for r in rows)
    cor = sum(r["correct"] for r in rows)
    conf = sum(r["confused"] for r in rows)
    gen_fail = sum(1 for r in rows if r["retrieved_ok"] and not r["correct"])

    print("\n  === 결과 (n={}, {}) ===".format(n, args.model))
    print("  검색 성공(@{})        : {:>3}/{:<3} {:.0f}%".format(
        args.top_k or config.TOP_K, ret, n, ret / n * 100))
    print("  최종 정답            : {:>3}/{:<3} {:.0f}%".format(cor, n, cor / n * 100))
    print("  검색OK·생성실패      : {:>3}/{:<3} {:.0f}%  ← 모델 한계".format(
        gen_fail, n, gen_fail / n * 100))
    print("  혼동(환각)           : {:>3}/{:<3} {:.0f}%".format(conf, n, conf / n * 100))
    print("  검색 지연(중앙값)     : {:.0f}ms".format(
        statistics.median(r["retrieval_ms"] for r in rows)))
    print("  체감 TTFT(중앙값)     : {:.2f}s".format(
        statistics.median(r["ttft_s"] for r in rows)))

    cats = {}
    for r in rows:
        cats.setdefault(r["cat"], []).append(r)
    print("\n  카테고리별 정답률:")
    for cat, rs in sorted(cats.items()):
        print("    {:<14} {:>3}문항  정답 {:>4.0f}%  검색 {:>4.0f}%".format(
            cat, len(rs),
            sum(x["correct"] for x in rs) / len(rs) * 100,
            sum(x["retrieved_ok"] for x in rs) / len(rs) * 100))


#------------------------------------------------------------------
# 명령행 진입점
#=> 평가셋·단계·모델을 골라 검색 또는 답변 평가를 돌리고 결과 JSON 을 저장한다.
#    1) --cases 로 문항 모듈을 늦게 불러 CASES 를 채운다
#    2) App 워밍업 후 리랭커 적재를 기다린다
#    3) --exp 를 주면 골든셋 실험 폴더(results/ragas_golden/<ID>/)에 같이 저장한다(계획서 0-2)
#
# -in: 없음 (sys.argv)
#
# -out: 0 = 성공
# -out: error = 모델·인덱스 적재 실패 시 예외 전파
#------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="확대 평가")
    p.add_argument("--cases", default="eval_cases",
                   help="문항 모듈 (eval_cases | eval_cases_v2)")
    p.add_argument("--stage", choices=["retrieval", "answer"], default="retrieval")
    p.add_argument("--model", default="qwen3-0.6b-q4")
    p.add_argument("--top-k", type=int, default=None,
                   help="sLLM 근거 수(생략 시 config.yaml generation.top_k)")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="답변 토큰 상한(생략 시 config.yaml generation.max_tokens)")
    p.add_argument("--exp", default="",
                   help="실험 ID — results/ragas_golden/<ID>/ 에 저장(골든셋 실험과 짝)")
    args = p.parse_args()
    
    # 문항 모듈을 늦게 불러 CASES 를 채운다.
    global CASES
    import importlib
    CASES = importlib.import_module(args.cases).CASES

    app = App(args.model)
    try:
        app.warmup(wait=True, skip_llm=(args.stage == "retrieval"))
        app.wait_rerank()        # 첫 문항 TTFT 에 백그라운드 리랭커 적재가 섞이지 않게(§34)
        print("[eval] {}문항 / stage={} / {}\n".format(
            len(CASES), args.stage, args.model))

        rows = (stage_retrieval(app, args) if args.stage == "retrieval"
                else stage_answer(app, args))

        tag = args.stage if args.stage == "retrieval" else args.stage + "_" + args.model
        out_dir = RESULTS_DIR
        if args.exp:
            # 골든셋 실험과 같은 폴더에 둬 한 실험의 결과를 한곳에서 본다
            out_dir = os.path.join(RESULTS_DIR, "ragas_golden", args.exp)
            os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "large_" + tag + ".json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print("\n[eval] 저장: " + out)
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
