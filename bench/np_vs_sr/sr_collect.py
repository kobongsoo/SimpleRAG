"""SimpleRAG 로 골든셋 문항을 질의해 답변·검색 근거를 JSON 으로 기록한다 (채점은 하지 않는다).

사용: SIMPLERAG_HOME=<sample_pdf 인덱스 폴더> .venv/Scripts/python sr_collect.py <golden.jsonl> <out.json>
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, r"D:\Project\SimpleRAG\src")


#------------------------------------------------------------------
# JSON 저장 (중간 결과 보존)
#=> 도중에 끊겨도 그때까지 문항이 남도록 임시 파일에 쓴 뒤 바꿔치기한다.
#
# -in: path = 저장 경로
# -in: obj  = 저장할 객체
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def save_json(path, obj):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


#------------------------------------------------------------------
# 수집 진입점
#=> 운영 설정(config.yaml) 그대로 App 을 띄워 문항마다 pipeline.answer 를 돌린다.
#    1) 워밍업·리랭커 적재를 먼저 끝내 첫 문항 시간이 튀지 않게 한다
#    2) evidence 이벤트의 청크 = sLLM 에 실제로 들어간 근거 → retrieved_contexts
#    3) 문항마다 파일을 갱신하고, 이미 있는 id 는 건너뛴다(이어하기)
#
# -in: 없음 (argv: 골든셋 경로, 출력 경로)
#
# -out: 없음 (출력 JSON)
# -out: error = 모델·인덱스 적재 실패 시 예외 전파
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from simplerag import config
    from simplerag.warmup import App

    golden = [json.loads(l) for l in io.open(sys.argv[1], encoding="utf-8") if l.strip()]
    out_path = sys.argv[2]
    doc = json.load(io.open(out_path, encoding="utf-8")) if os.path.exists(out_path) else None
    done_ids = {r["id"] for r in doc["items"]} if doc else set()

    app = App(config.DEFAULT_GEN_MODEL)
    try:
        t_warm = time.time()
        app.warmup(wait=True)
        app.wait_rerank()
        if doc is None:
            doc = {"product": "SimpleRAG", "index_home": config.ROOT,
                   "settings": {"gen_model": config.DEFAULT_GEN_MODEL, "top_k": config.TOP_K,
                                "rerank_pool": config.RERANK_POOL, "rerank": bool(config.RERANK),
                                "backend": getattr(getattr(app, "generator", None), "backend", None)},
                   "warmup_sec": round(time.time() - t_warm, 2),
                   "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": []}
        print("[SR] {}문항 / 모델 {} / 근거 {}건 / 리랭커 {}".format(
            len(golden), config.DEFAULT_GEN_MODEL, config.TOP_K, config.RERANK), flush=True)
        for i, g in enumerate(golden, 1):
            if g["id"] in done_ids:
                continue
            chunks, done = [], None
            # 스트림 조각 1개 = 생성 토큰 1개(llama-cpp stream) — 첫·끝 시각으로 디코드 속도를 잰다
            n_tok, t_tok0, t_tok1 = 0, None, None
            for ev in app.pipeline.answer(g["user_input"]):
                if ev[0] == "evidence":
                    chunks = ev[1]
                elif ev[0] == "token":
                    t_tok1 = time.perf_counter()
                    t_tok0 = t_tok0 or t_tok1
                    n_tok += 1
                elif ev[0] == "done":
                    done = ev[1]
            gen_s = (t_tok1 - t_tok0) if n_tok > 1 else None
            names = [c.get("doc_name", "") for c in chunks]
            doc["items"].append({
                **{k: g[k] for k in ("id", "user_input", "reference", "reference_contexts", "reference_pages",
                                     "doc_ids", "source_files", "question_type", "difficulty", "must")},
                "response": done["answer"],
                "retrieved_contexts": [c["text"] for c in chunks],
                "retrieved_meta": [{"doc": c.get("doc_name"), "chunk_idx": c.get("chunk_idx"),
                                    "section_id": c.get("section_id")} for c in chunks],
                # 사본(두 파일) 중 어느 쪽이 검색돼도 정답 문서로 본다
                "doc_hit": any(n in g["source_files"] for n in names),
                "cited": done.get("cited", []),
                # ttft_s = 질문→첫 글자(검색 포함, 사용자 체감) / llm_ttft_s = 생성 시작→첫 토큰
                # tok_s = (토큰수-1) ÷ 첫 토큰→마지막 토큰 시간 (디코드 속도, llama-server 의 predicted_per_second 와 같은 정의)
                "timing": {"ttft_s": done.get("ttft_s"), "total_s": done.get("total_s"),
                           "llm_ttft_s": done.get("llm_ttft_s"),
                           "retrieval_ms": (done.get("timing") or {}).get("total_ms"),
                           "tokens": n_tok, "gen_s": None if gen_s is None else round(gen_s, 3),
                           "tok_s": round((n_tok - 1) / gen_s, 2) if gen_s else None},
            })
            save_json(out_path, doc)
            tm = doc["items"][-1]["timing"]
            print("  [{:>3}] {} TTFT {:.2f}s {}tok {}tok/s total {:.1f}s {}".format(
                i, "R" if doc["items"][-1]["doc_hit"] else "-", tm["ttft_s"] or 0, n_tok, tm["tok_s"],
                tm["total_s"] or 0, g["user_input"][:30]), flush=True)
    finally:
        app.close()
    doc["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(out_path, doc)
    print("[SR] 저장:", out_path)


if __name__ == "__main__":
    main()
