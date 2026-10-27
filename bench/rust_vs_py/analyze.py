"""perf.py 결과 분석 — 두 판 속도·메모리 비교, 두 판 답 일치, 반복 루프 수정 전후 비교.

수정 전 기준선: results/np_vs_sr/simplerag_responses.json (2026-10-23, Python 소스, 같은 config·모델·sample_pdf)
사용: .venv\\Scripts\\python bench/rust_vs_py/analyze.py
결과: results/rust_vs_py/analysis.json + 표준출력 요약
"""
import io
import json
import os
import re
import statistics as st
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
R = os.path.join(ROOT, "results", "rust_vs_py")
BASE = os.path.join(ROOT, "results", "np_vs_sr", "simplerag_responses.json")
GGUF = os.path.join(ROOT, "dist", "simplerag", "models", "Qwen3-1.7B-Q4_K_M.gguf")
CUT_NOTE = "(답변이 길어 일부 생략)"
CAP = 200


#------------------------------------------------------------------
# 토큰 수 세기 (생성 모델 토크나이저)
#=> chat 출력에는 토큰 수가 없어서, 답변을 같은 Qwen3 토크나이저로 다시 세어 tok/s 를 근사한다.
#   (어휘만 읽어 가볍다. 후처리로 지운 글자는 빠지므로 실제 생성 토큰보다 조금 적을 수 있다)
#
# -in: 없음
#
# -out: count(text) 함수
# -out: error = 모델 파일이 없으면 예외 전파
#------------------------------------------------------------------
def token_counter():
    from llama_cpp import Llama
    llm = Llama(model_path=GGUF, vocab_only=True, verbose=False)
    return lambda s: len(llm.tokenize((s or "").encode("utf-8"), add_bos=False))


#------------------------------------------------------------------
# 반복 흔적 찾기
#=> 같은 40자 이상 덩어리가 답 안에 두 번 나오면 반복 루프로 본다(수정 규칙과 같은 문턱).
#
# -in: text = 답변
#
# -out: True = 반복 있음
# -out: error = 없음
#------------------------------------------------------------------
def has_repeat(text, n=40):
    t = re.sub(r"\s+", " ", text or "")
    seen = set()
    for i in range(0, max(0, len(t) - n + 1)):
        s = t[i:i + n]
        if s in seen:
            return True
        seen.add(s)
    return False


#------------------------------------------------------------------
# 정답 핵심어 포함
#=> must 핵심어가 모두 답에 들어 있는가(공백·쉼표·별표 무시).
#
# -in: ans = 답변, must = 핵심어 목록
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def must_hit(ans, must):
    n = lambda s: re.sub(r"[\s,*]", "", s or "")
    return all(n(m) in n(ans) for m in must)


#------------------------------------------------------------------
# 중앙값·p90
#=> None 을 뺀 값으로 계산한다.
#
# -in: xs = 숫자 목록
#
# -out: (중앙값, p90) / 값이 없으면 (None, None)
# -out: error = 없음
#------------------------------------------------------------------
def mp(xs):
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None, None
    return round(st.median(v), 2), round(v[max(0, int(len(v) * 0.9) - 1)], 2)


#------------------------------------------------------------------
# 진입점
#=> 속도·메모리 요약, 두 판 답 비교, 수정 전후 비교를 계산해 저장·출력한다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 결과 파일 없으면 예외 전파
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    perf = json.load(io.open(os.path.join(R, "perf.json"), encoding="utf-8"))
    chat = {p: {x["id"]: x for x in json.load(io.open(os.path.join(R, "chat_%s.json" % p), encoding="utf-8"))["items"]}
            for p in ("python", "rust")}
    base = {x["id"]: x for x in json.load(io.open(BASE, encoding="utf-8"))["items"]}
    count = token_counter()
    out = {"perf": {}, "same": {}, "fix": {}}

    # ── 속도 · 메모리 ──
    for p in ("python", "rust"):
        it = list(chat[p].values())
        toks = [count(x["answer"]) for x in it]
        tps = [(t - 1) / (x["total_s"] - x["ttft_s"]) for t, x in zip(toks, it)
               if x["total_s"] and x["ttft_s"] is not None and x["total_s"] - x["ttft_s"] > 0.3 and t > 1]
        idx = perf["index"][p]
        wu = perf["warmup"][p]
        out["perf"][p] = {
            "index_s": [r["index_s"] for r in idx], "index_peak_mb": max(r["peak_mb"] for r in idx),
            "warmup_wall_s": [r["wall_s"] for r in wu], "warmup_ready_ms": [r["ready_ms"] for r in wu],
            "warmup_peak_mb": max(r["peak_mb"] for r in wu),
            "chat_ready_s": perf["chat"][p]["ready_s"], "chat_idle_mb": perf["chat"][p]["idle_mb"],
            "chat_peak_mb": perf["chat"][p]["peak_mb"],
            "ttft_s": mp([x["ttft_s"] for x in it]), "total_s": mp([x["total_s"] for x in it]),
            "screen_first_s": mp([x["ext_first_s"] for x in it]), "retrieval_ms": mp([x["retrieval_ms"] for x in it]),
            "tok_s": mp(tps), "tokens": mp(toks), "size": perf["size"][p]}
    # ── 두 판 답·근거 일치 ──
    ids = sorted(chat["python"])
    same_ans = [q for q in ids if chat["python"][q]["answer"] == chat["rust"][q]["answer"]]
    same_ev = [q for q in ids if [e["text"] for e in chat["python"][q]["evidence"]] ==
               [e["text"] for e in chat["rust"][q]["evidence"]]]
    out["same"] = {"n": len(ids), "answer_same": len(same_ans), "evidence_same": len(same_ev),
                   "answer_diff_ids": [q for q in ids if q not in same_ans],
                   "evidence_diff_ids": [q for q in ids if q not in same_ev]}
    # ── 반복 수정 전후 ──
    for p in ("python", "rust"):
        b_cap = [q for q in ids if (base[q]["timing"].get("tokens") or 0) >= CAP]
        b_rep = [q for q in ids if has_repeat(base[q]["response"])]
        a_cut = [q for q in ids if CUT_NOTE in (chat[p][q]["answer"] or "")]
        a_rep = [q for q in ids if has_repeat((chat[p][q]["answer"] or "").replace(CUT_NOTE, ""))]
        changed = [q for q in ids if (chat[p][q]["answer"] or "").replace(CUT_NOTE, "").strip()
                   != base[q]["response"].strip()]
        out["fix"][p] = {
            "before_cap_hit": len(b_cap), "before_repeat": len(b_rep), "before_cap_ids": b_cap, "before_repeat_ids": b_rep,
            "after_cut_note": len(a_cut), "after_repeat": len(a_rep), "after_cut_ids": a_cut, "after_repeat_ids": a_rep,
            "answers_changed": len(changed), "changed_ids": changed,
            "must_before": sum(must_hit(base[q]["response"], base[q]["must"]) for q in ids),
            "must_after": sum(must_hit(chat[p][q]["answer"], chat[p][q]["must"]) for q in ids),
            "total_before": mp([base[q]["timing"]["total_s"] for q in ids]),
            "total_after": mp([chat[p][q]["total_s"] for q in ids]),
            "tokens_before": mp([base[q]["timing"].get("tokens") for q in ids]),
        }
    json.dump(out, io.open(os.path.join(R, "analysis.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
