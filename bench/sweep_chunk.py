#------------------------------------------------------------------
# 청크 크기 스윕 — 정답률까지 비교 (설계서 결정3 재검증)
#=> 128 은 '속도 제약 아래에서 검색 손해가 없는 가장 빠른 값' 으로 골랐다.
#   그런데 **청크 크기별 최종 정답률은 측정한 적이 없다.** 검색 Recall 과
#   TTFT 만 봤을 뿐이다. 이 스크립트가 그 빈틈을 메운다.
#
#   각 크기마다 별도 데이터 루트에 새로 인덱싱하고 45문항을 돌린다.
#   기존 128 인덱스는 건드리지 않는다(SIMPLERAG_HOME 으로 분리).
#
#   ⚠️ 오래 걸린다. 크기당 인덱싱 20~25분 + 평가 5~10분.
#
#   사용:
#     python bench/sweep_chunk.py --sizes 192 256
#     python bench/sweep_chunk.py --sizes 192 --skip-index   # 이미 만든 인덱스 재평가
#------------------------------------------------------------------

import argparse
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, ".."))
RESULTS = os.path.join(ROOT, "results")
EXP = os.path.join(ROOT, "exp")
PY = sys.executable
DOC_DIR = r"D:\분류함"


#------------------------------------------------------------------
# 실험용 환경변수 구성
#=> 데이터 루트만 분리하고 모델은 원래 자리를 그대로 쓴다(1.5GB 를 복사하지 않기 위함).
#
# -in: size = 청크 토큰 수
#
# -out: dict = subprocess 에 넘길 환경변수
#------------------------------------------------------------------
def make_env(size):
    env = dict(os.environ)
    env["SIMPLERAG_HOME"] = os.path.join(EXP, "chunk{}".format(size))
    env["SIMPLERAG_MODELS_DIR"] = os.path.join(ROOT, "models")
    env["SIMPLERAG_CHUNK_TOKENS"] = str(size)
    env["SIMPLERAG_CHUNK_OVERLAP"] = str(max(8, size // 8))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


#------------------------------------------------------------------
# 하위 명령 실행 (출력은 파일로, 마지막 줄만 화면에)
#------------------------------------------------------------------
def run(cmd, env, log_path, tail=6):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, env=env, cwd=ROOT, stdout=f,
                           stderr=subprocess.STDOUT)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip() for l in f if l.strip()]
    for l in lines[-tail:]:
        print("    " + l)
    return r.returncode


#------------------------------------------------------------------
# 크기 1개 처리: 인덱싱 → 검색평가 → 답변평가
#------------------------------------------------------------------
def sweep_one(size, args):
    home = os.path.join(EXP, "chunk{}".format(size))
    env = make_env(size)
    print("\n{}".format("=" * 66))
    print("청크 {}토큰 (겹침 {})  데이터루트={}".format(
        size, max(8, size // 8), home))
    print("=" * 66)

    if not args.skip_index:
        print("  [1/3] 인덱싱...")
        t0 = time.perf_counter()
        rc = run([PY, "-u", os.path.join("src", "simplerag", "cli.py"),
                  "index", "--dir", DOC_DIR, "--rebuild"],
                 env, os.path.join(EXP, "log", "index{}.log".format(size)))
        print("    → {:.0f}분".format((time.perf_counter() - t0) / 60))
        if rc != 0:
            print("    인덱싱 실패(rc={})".format(rc))
            return None

    print("  [2/3] 검색 평가...")
    run([PY, "-u", os.path.join("bench", "eval_large.py"), "--stage", "retrieval"],
        env, os.path.join(EXP, "log", "ret{}.log".format(size)), tail=12)

    print("  [3/3] 답변 평가...")
    run([PY, "-u", os.path.join("bench", "eval_large.py"),
         "--stage", "answer", "--model", args.model],
        env, os.path.join(EXP, "log", "ans{}.log".format(size)), tail=14)

    # 결과 JSON 을 크기별로 보관(eval_large 는 results/ 에 덮어쓴다).
    out = {}
    for stage, src in (("retrieval", "large_retrieval.json"),
                       ("answer", "large_answer_{}.json".format(args.model))):
        p = os.path.join(home, "results", src)
        if not os.path.isfile(p):
            p = os.path.join(RESULTS, src)
        if os.path.isfile(p):
            dst = os.path.join(RESULTS, "sweep_{}_{}.json".format(stage, size))
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            out[stage] = data
    return out


#------------------------------------------------------------------
# 청크 수 조회
#=> 인덱싱 로그의 마지막 요약줄에서 뽑는다. 128 은 본 인덱스 값(고정).
#
# -in: size = 청크 토큰 수
#
# -out: str = 자릿수 구분 문자열, 모르면 "-"
#------------------------------------------------------------------
def chunk_count(size):
    if size == 128:
        return "43,058"
    log = os.path.join(EXP, "log", "index{}.log".format(size))
    if not os.path.isfile(log):
        # exp/ 는 용량이 커서 실험 후 지운다. 로그 사본만 results/ 에 남긴다.
        log = os.path.join(RESULTS, "sweep_logs", "index{}.log".format(size))
    if not os.path.isfile(log):
        return "-"
    import re
    n = None
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.search(r"청크 ([\d,]+)개", line)
            if m:
                n = m.group(1)
    return n or "-"


#------------------------------------------------------------------
# 요약표 출력
#=> 128(기존 결과)과 새로 잰 크기들을 한 표에 놓는다.
#------------------------------------------------------------------
def summarize(sizes, model):
    print("\n\n{}".format("=" * 78))
    print(" 청크 크기별 비교 (45문항, {})".format(model))
    print("=" * 78)
    print("  {:<8} {:>8} {:>9} {:>9} {:>9} {:>9} {:>8}".format(
        "청크", "청크수", "Recall@1", "Recall@3", "정답률", "혼동", "TTFT"))
    print("  " + "-" * 74)

    rows = []
    for size in sizes:
        ret_p = os.path.join(RESULTS, "sweep_retrieval_{}.json".format(size))
        ans_p = os.path.join(RESULTS, "sweep_answer_{}.json".format(size))
        if size == 128:      # 기존 본 실행 결과(백업본)를 쓴다
            # ⚠️ eval_large 는 SIMPLERAG_HOME 과 무관하게 results/ 에 덮어쓴다.
            #    그래서 스윕을 돌리면 128 기준선이 날아간다. baseline128/ 에
            #    떠 둔 사본을 먼저 찾고, 없을 때만 원본을 본다.
            base = os.path.join(RESULTS, "baseline128")
            ret_p = os.path.join(base, "large_retrieval.json")
            ans_p = os.path.join(base, "large_answer_{}.json".format(model))
            if not os.path.isfile(ret_p):
                ret_p = os.path.join(RESULTS, "large_retrieval.json")
            if not os.path.isfile(ans_p):
                ans_p = os.path.join(RESULTS, "large_answer_{}.json".format(model))
        if not (os.path.isfile(ret_p) and os.path.isfile(ans_p)):
            print("  {:<8} (결과 없음)".format(size))
            continue

        with open(ret_p, encoding="utf-8") as f:
            ret = json.load(f)
        with open(ans_p, encoding="utf-8") as f:
            ans = json.load(f)

        import statistics
        n = len(ans)
        row = {
            "size": size,
            "recall1": sum(r["hit1"] for r in ret) / len(ret),
            "recall3": sum(r["hit3"] for r in ret) / len(ret),
            "acc": sum(r["correct"] for r in ans) / n,
            "conf": sum(r["confused"] for r in ans) / n,
            "ttft": statistics.median(r["ttft_s"] for r in ans),
        }
        rows.append(row)
        print("  {:<8} {:>8} {:>8.0f}% {:>8.0f}% {:>8.0f}% {:>8.0f}% {:>7.2f}s".format(
            size, chunk_count(size), row["recall1"] * 100, row["recall3"] * 100,
            row["acc"] * 100, row["conf"] * 100, row["ttft"]))

    # 카테고리별 정답률 — 표 문서(경조사)가 크기에 민감할 것으로 예상된다.
    if rows:
        print("\n  카테고리별 정답률")
        cats = None
        table = {}
        for size in sizes:
            if size == 128:
                p = os.path.join(RESULTS, "baseline128",
                                 "large_answer_{}.json".format(model))
                if not os.path.isfile(p):
                    p = os.path.join(RESULTS,
                                     "large_answer_{}.json".format(model))
            else:
                p = os.path.join(RESULTS, "sweep_answer_{}.json".format(size))
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                ans = json.load(f)
            per = {}
            for r in ans:
                per.setdefault(r["cat"], []).append(r["correct"])
            table[size] = {c: sum(v) / len(v) for c, v in per.items()}
            cats = sorted(per) if cats is None else cats

        if cats:
            print("  {:<12}".format("") + "".join(
                "{:>10}".format(s) for s in table))
            for c in cats:
                print("  {:<12}".format(c) + "".join(
                    "{:>9.0f}%".format(table[s].get(c, 0) * 100) for s in table))

    out = os.path.join(RESULTS, "sweep_chunk_summary.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("\n[sweep] 저장: " + out)


def main():
    p = argparse.ArgumentParser(description="청크 크기 스윕")
    p.add_argument("--sizes", nargs="+", type=int, default=[192, 256])
    p.add_argument("--model", default="qwen3-0.6b-q4")
    p.add_argument("--skip-index", action="store_true")
    p.add_argument("--summary-only", action="store_true")
    args = p.parse_args()

    if not args.summary_only:
        for size in args.sizes:
            sweep_one(size, args)

    summarize([128] + list(args.sizes), args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
