"""바꿔 말한 질문 세트로 검색 놓침 재기 — 같은 문항을 '문서 용어 질문'과 '일상어 질문'으로 각각 검색해 비교한다.

운영과 같은 검색(dense + BM25 → RRF → 리랭킹 → 근거 3건)만 돌린다(LLM 생성 없음).
사용: SIMPLERAG_HOME=<sample_pdf 인덱스 폴더> .venv\\Scripts\\python bench/paraphrase/measure.py [출력 json]
"""
import io
import json
import os
import re
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
SET = os.path.join(ROOT, "dataset", "paraphrase_pdf30.jsonl")


#------------------------------------------------------------------
# 공백 지운 글
#=> 청크와 발췌의 줄바꿈·띄어쓰기 차이를 무시하고 비교하려고.
#
# -in: s = 글
#
# -out: 공백 없는 글
# -out: error = 없음
#------------------------------------------------------------------
def _n(s):
    return re.sub(r"\s+", "", s or "")


#------------------------------------------------------------------
# 문서 이름 맞추기용 줄기
#=> 확장자와 끝의 사본 번호("-1")를 뗀다 — sample_pdf 를 폴더로 정리하며 "취업규칙.pdf" 가
#   "취업규칙/취업규칙-1.pdf" 로 바뀌었다(내용은 글자까지 같음, 2026-10-27 확인).
#
# -in: name = 파일 이름
#
# -out: 비교용 이름
# -out: error = 없음
#------------------------------------------------------------------
def _stem(name):
    return re.sub(r"-\d+$", "", os.path.splitext(os.path.basename(name or ""))[0])


#------------------------------------------------------------------
# 정답 근거가 검색 결과에 들었나
#=> 발췌마다, 그 발췌가 통째로(공백 무시) 청크에 있거나 — 발췌가 청크 경계에 걸쳐 잘렸을 수 있어 —
#   발췌의 가운데 30자가 청크에 있으면 찾은 것으로 본다. 발췌 하나라도 찾으면 적중.
#
# -in: contexts = 정답 발췌 목록 / chunks = 검색된 청크 본문 목록
#
# -out: True = 적중
# -out: error = 없음
#------------------------------------------------------------------
def ctx_hit(contexts, chunks):
    cs = [_n(c) for c in chunks]
    for ctx in contexts:
        x = _n(ctx)
        mid = x[max(0, len(x) // 2 - 15):len(x) // 2 + 15] if len(x) > 40 else x
        if any(x in c or mid in c for c in cs):
            return True
    return False


#------------------------------------------------------------------
# 진입점
#=> 문항마다 두 질문(문서 용어 · 일상어)을 검색해 정답 근거 적중·정답 문서 적중을 세고 결과를 저장한다.
#
# -in: 없음 (argv: 출력 경로, 기본 results/paraphrase/measure.json)
#
# -out: 없음
# -out: error = 인덱스·모델 적재 실패 시 예외 전파
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from simplerag import config
    from simplerag.warmup import App

    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "results", "paraphrase", "measure.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    rows = [json.loads(l) for l in io.open(SET, encoding="utf-8") if l.strip()]
    app = App(config.DEFAULT_GEN_MODEL)
    app.warmup(wait=True, skip_llm=True)
    app.wait_rerank()
    items = []
    for r in rows:
        rec = {k: r[k] for k in ("id", "origin_id", "user_input", "original_input", "swaps", "source_files")}
        for key, q in (("orig", r["original_input"]), ("para", r["user_input"])):
            t = time.perf_counter()
            chunks, timing = app.pipeline.retriever.search(q, top_k=config.TOP_K)
            rec[key] = {"ctx_hit": ctx_hit(r["reference_contexts"], [c["text"] for c in chunks]),
                        "doc_hit": any(_stem(c.get("doc_name")) in {_stem(f) for f in r["source_files"]}
                                       for c in chunks),
                        "docs": [c.get("doc_name") for c in chunks],
                        "no_relevant": bool(timing.get("no_relevant")),
                        "ms": round((time.perf_counter() - t) * 1000)}
        items.append(rec)
        o, p = rec["orig"], rec["para"]
        print("{:<7} 문서용어 {}{}  일상어 {}{}  {}".format(
            r["id"], "근" if o["ctx_hit"] else "-", "문" if o["doc_hit"] else "-",
            "근" if p["ctx_hit"] else "-", "문" if p["doc_hit"] else "-", r["user_input"][:32]), flush=True)
    app.close()
    n = len(items)
    summ = {k: {m: sum(x[k][m] for x in items) for m in ("ctx_hit", "doc_hit", "no_relevant")} for k in ("orig", "para")}
    lost = [x["id"] for x in items if x["orig"]["ctx_hit"] and not x["para"]["ctx_hit"]]
    json.dump({"n": n, "summary": summ, "lost": lost, "items": items},
              io.open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("문항 {} · 문서 용어 질문: 근거 적중 {} 문서 적중 {} · 일상어 질문: 근거 적중 {} 문서 적중 {} · 일상어로 바꿔 놓친 문항 {}".format(
        n, summ["orig"]["ctx_hit"], summ["orig"]["doc_hit"], summ["para"]["ctx_hit"], summ["para"]["doc_hit"], len(lost)))


if __name__ == "__main__":
    main()
