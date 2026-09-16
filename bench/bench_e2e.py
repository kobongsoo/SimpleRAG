#------------------------------------------------------------------
# 종단간(End-to-End) 실측 — 확정 설계 조합 그대로
#=> 지금까지의 숫자는 단계별로 따로 잰 것이라, 조합했을 때의 TTFT 를 그대로
#   믿을 수 없다. 특히 V3 프롬프트는 V0 보다 지시문이 길어 prefill 이 늘어난다.
#   이 스크립트는 실제 파이프라인을 그대로 태워서 한 번에 잰다.
#
#   경로: 질의 -> 쿼리임베딩 -> Qdrant 검색 + BM25 -> RRF -> top-3
#         -> V3 프롬프트 조립 -> llama.cpp 스트리밍 -> TTFT / 완료
#
#   확정 설계
#     생성   Qwen3-0.6B-Q4_K_M, threads=8, flash_attn=True, n_ctx=2048
#     프롬프트 V3_결합
#     임베딩 e5-small-ko ONNX int8, threads=4
#     검색   Qdrant local (5만 청크) + BM25, RRF 융합, top-3
#     청킹   128토큰
#
#   사용:  python bench/bench_e2e.py
#------------------------------------------------------------------

import argparse
import json
import os
import statistics
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from bench_llm import MODELS, MODELS_DIR, RESULTS_DIR, build_prompt, load_llm  # noqa: E402
from bench_quality import PROMPTS                                              # noqa: E402
from bench_retrieval import (BatchOnnxEmbedder, QDRANT_DIR, QUERY_PREFIX,      # noqa: E402
                             make_corpus)

QUERIES = [
    "연차휴가 미사용 수당은 언제 지급되나요?",
    "재택근무는 주 며칠까지 가능한가요?",
    "출장비 정산 기준을 알려주세요.",
    "보안 사고가 나면 어디에 신고해야 하나요?",
    "사용촉진 절차는 어떻게 되나요?",
    "가산휴가를 포함한 총 한도는 며칠인가요?",
]


#------------------------------------------------------------------
# RRF 융합
#=> 점수 체계가 다른 두 검색(dense/BM25)을 순위만으로 합친다.
#   점수 정규화가 필요 없어 하이브리드 융합의 기본값으로 쓰인다.
#     score(d) = sum over lists of 1 / (k + rank(d))
#
# -in: ranked_lists = [[doc_id 순위대로], ...]
# -in: k            = 순위 완충 상수(관례적으로 60)
#
# -out: [(doc_id, score), ...] 점수 내림차순
#------------------------------------------------------------------
def rrf(ranked_lists, k=60):
    scores = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def main():
    p = argparse.ArgumentParser(description="종단간 실측")
    p.add_argument("--model", default="qwen3-0.6b-q4")
    p.add_argument("--variant", default="V3_결합")
    p.add_argument("--llm-threads", type=int, default=8)
    p.add_argument("--embed-threads", type=int, default=4)
    p.add_argument("--n-ctx", type=int, default=2048)
    p.add_argument("--max-tokens", type=int, default=200)
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--first-stage", type=int, default=10, help="융합 전 각 검색기 후보 수")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--n", type=int, default=50000)
    p.add_argument("--chunk-tokens", type=int, default=128)
    args = p.parse_args()

    from qdrant_client import QdrantClient
    from rank_bm25 import BM25Okapi

    print("[e2e] 모델={} 프롬프트={} top-{}\n".format(args.model, args.variant, args.top_k))

    # --- 구성요소 적재 (콜드스타트 비용도 함께 본다) ---
    t0 = time.perf_counter()
    emb = BatchOnnxEmbedder(num_threads=args.embed_threads)
    t_emb_load = time.perf_counter() - t0

    t0 = time.perf_counter()
    client = QdrantClient(path=QDRANT_DIR)
    t_qdrant_load = time.perf_counter() - t0

    # BM25 는 인덱스를 파일로 두지 않았으므로 코퍼스를 재생성해 구축한다
    # (bench_retrieval 과 같은 시드/규칙이라 내용이 동일하다).
    t0 = time.perf_counter()
    corpus = make_corpus(args.n, args.chunk_tokens, emb.tok)
    bm = BM25Okapi([c.split() for c in corpus])
    t_bm25_load = time.perf_counter() - t0

    t0 = time.perf_counter()
    llm, _ = load_llm(os.path.join(MODELS_DIR, MODELS[args.model]),
                      args.llm_threads, args.n_ctx)
    t_llm_load = time.perf_counter() - t0

    print("  콜드스타트: 임베더 {:.2f}s / Qdrant {:.2f}s / BM25 {:.2f}s / LLM {:.2f}s "
          "= 합계 {:.2f}s".format(t_emb_load, t_qdrant_load, t_bm25_load, t_llm_load,
                                t_emb_load + t_qdrant_load + t_bm25_load + t_llm_load))

    system = PROMPTS[args.variant]
    sys_tokens = len(llm.tokenize(system.encode("utf-8"), add_bos=False))
    print("  시스템 프롬프트: {} tok ({})\n".format(sys_tokens, args.variant))

    rows = []
    for rep in range(args.repeat):
        for q in QUERIES:
            # (1) 쿼리 임베딩
            t0 = time.perf_counter()
            qv = emb.embed([q], prefix=QUERY_PREFIX)[0]
            t_qe = time.perf_counter()

            # (2) dense 검색
            hits = client.query_points(collection_name="rag", query=qv.tolist(),
                                       limit=args.first_stage, with_payload=True)
            dense_ids = [h.id for h in hits.points]
            t_dense = time.perf_counter()

            # (3) BM25 검색
            bm_scores = bm.get_scores(q.split())
            bm_ids = list(np.argsort(bm_scores)[::-1][:args.first_stage])
            t_bm = time.perf_counter()

            # (4) RRF 융합 -> top-k
            fused = rrf([dense_ids, [int(i) for i in bm_ids]])[:args.top_k]
            texts = [corpus[int(doc_id)] for doc_id, _ in fused]
            t_fuse = time.perf_counter()

            # (5) 프롬프트 조립
            user = ("[근거]\n" + "\n\n".join(
                "[{}] {}".format(i + 1, t) for i, t in enumerate(texts))
                + "\n\n[질문]\n" + q)
            prompt = build_prompt(system, user)
            n_prompt = len(llm.tokenize(prompt.encode("utf-8"), add_bos=True))

            # (6) 생성 — 스트리밍으로 TTFT 직접 측정
            llm.reset()
            t_gen0 = time.perf_counter()
            t_first = None
            for _ in llm.create_completion(prompt, max_tokens=args.max_tokens,
                                           temperature=0.0, stop=["<|im_end|>"],
                                           stream=True):
                if t_first is None:
                    t_first = time.perf_counter()
            t_end = time.perf_counter()
            if t_first is None:
                t_first = t_end
            n_gen = llm.n_tokens - n_prompt

            retrieval_ms = (t_fuse - t0) * 1000
            rows.append({
                "query": q,
                "prompt_tokens": n_prompt,
                "gen_tokens": n_gen,
                "q_embed_ms": round((t_qe - t0) * 1000, 1),
                "dense_ms": round((t_dense - t_qe) * 1000, 1),
                "bm25_ms": round((t_bm - t_dense) * 1000, 1),
                "fuse_ms": round((t_fuse - t_bm) * 1000, 1),
                "retrieval_ms": round(retrieval_ms, 1),
                "llm_ttft_s": round(t_first - t_gen0, 3),
                # 사용자가 체감하는 값: 검색까지 포함해 첫 글자가 나오기까지
                "user_ttft_s": round(retrieval_ms / 1000 + (t_first - t_gen0), 3),
                "total_s": round(retrieval_ms / 1000 + (t_end - t_gen0), 3),
            })

    def med(key):
        return statistics.median(r[key] for r in rows)

    print("  {:<38} {:>6} {:>8} {:>9} {:>9}".format(
        "질의", "프롬프트", "검색ms", "TTFT(s)", "완료(s)"))
    for q in QUERIES:
        sub = [r for r in rows if r["query"] == q]
        print("  {:<38} {:>6} {:>8.1f} {:>9.2f} {:>9.2f}".format(
            q[:36], sub[-1]["prompt_tokens"],
            statistics.median(r["retrieval_ms"] for r in sub),
            statistics.median(r["user_ttft_s"] for r in sub),
            statistics.median(r["total_s"] for r in sub)))

    print("\n  === 중앙값 ===")
    print("  프롬프트 토큰   : {:.0f} tok".format(med("prompt_tokens")))
    print("  검색 단계       : {:.0f} ms  (임베딩 {:.0f} + dense {:.0f} + BM25 {:.0f} + 융합 {:.0f})".format(
        med("retrieval_ms"), med("q_embed_ms"), med("dense_ms"), med("bm25_ms"), med("fuse_ms")))
    print("  LLM TTFT        : {:.2f} s".format(med("llm_ttft_s")))
    print("  체감 TTFT(검색+) : {:.2f} s".format(med("user_ttft_s")))
    print("  전체 완료       : {:.2f} s  (생성 {:.0f} tok)".format(
        med("total_s"), med("gen_tokens")))

    out = os.path.join(RESULTS_DIR, "e2e.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"config": vars(args),
                   "cold_start_s": {"embedder": round(t_emb_load, 2),
                                    "qdrant": round(t_qdrant_load, 2),
                                    "bm25": round(t_bm25_load, 2),
                                    "llm": round(t_llm_load, 2)},
                   "system_prompt_tokens": sys_tokens,
                   "rows": rows}, f, ensure_ascii=False, indent=2)
    print("\n[e2e] 저장: " + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
