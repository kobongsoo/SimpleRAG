#------------------------------------------------------------------
# 실문서 검증 (설계서 R1 — 가장 높은 리스크)
#=> 지금까지의 검색 측정은 합성 코퍼스 위에서 했다. 합성 코퍼스는 작은 문장
#   풀을 회전시켜 만든 것이라 근사 중복 청크가 많아, 검색 '정확도'는 전혀
#   검증되지 않았다. 실제 사내규정 문서로 다시 확인한다.
#
#   검증 항목
#    1) 검색 품질 — 질문의 정답이 든 문서가 top-k 안에 들어오는가 (Recall@k)
#       dense 단독 / BM25 단독 / 하이브리드(RRF) 를 나눠 재서 하이브리드의
#       실익을 확인한다.
#    2) 청크 크기 — 128 / 192 / 256 중 어디가 검색 품질이 좋은가.
#       (설계서 결정3 은 속도만 보고 128 을 골랐다. 품질 대가를 확인해야 한다)
#    3) 답변 품질 — 실제 검색 결과로 생성했을 때 정답/혼동
#    4) 실코퍼스 기준 인덱싱·질의 시간
#
#   이 코퍼스는 지원금액이 제각각(100만/200만/20만/50만/10만원)이라
#   숫자 혼동을 잡아내기에 좋은 조건이다.
#
#   사용:
#     python bench/bench_real.py --stage retrieval   # 검색 품질 (청크 크기 비교)
#     python bench/bench_real.py --stage answer      # 답변 품질 + E2E
#------------------------------------------------------------------

import argparse
import json
import os
import re
import statistics
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
RESULTS_DIR = os.path.abspath(os.path.join(_HERE, "..", "results"))
CSO_SRC = r"D:\Project\CSOClassify\src"
DOC_DIR = r"D:\분류함\2.회사규정"

from bench_retrieval import BatchOnnxEmbedder, PASSAGE_PREFIX, QUERY_PREFIX  # noqa: E402
from bench_e2e import rrf                                                    # noqa: E402

# 평가 문항 — 실제 문서에서 확인한 사실만 사용한다.
#   src      : 정답이 든 문서 파일명 조각(검색 정확도 채점용)
#   must     : 답변에 있어야 할 표현. "a|b" 는 둘 중 하나면 인정.
#   must_not : 나오면 혼동으로 보는 표현(다른 제도의 금액 등)
CASES = [
    {"q": "리프레시 휴가비는 얼마인가요?", "src": "42.리프레시",
     "must": ["100만원"], "must_not": ["200만원", "2백만원", "20만원"]},
    {"q": "리프레시 휴가는 몇 년에 한 번 신청할 수 있나요?", "src": "42.리프레시",
     "must": ["3년"], "must_not": ["1년에 1회"]},
    {"q": "리프레시 휴가를 신청하려면 몇 년 이상 근속해야 하나요?", "src": "42.리프레시",
     "must": ["2년"], "must_not": ["3개월"]},
    {"q": "학자금은 자녀 한 명당 연간 얼마까지 지원되나요?", "src": "28.학자금",
     "must": ["2백만원|200만원"], "must_not": ["100만원", "20만원"]},
    {"q": "초등학교 입학 축하금은 얼마인가요?", "src": "24.초등학교",
     "must": ["20만원"], "must_not": ["100만원", "200만원", "2백만원"]},
    {"q": "의료비는 본인부담금이 얼마를 넘을 때 지원되나요?", "src": "15.의료비",
     "must": ["50만원"], "must_not": ["100만원", "25만원"]},
    {"q": "의료비 지원을 받으려면 몇 개월 이상 근속해야 하나요?", "src": "15.의료비",
     "must": ["3개월"], "must_not": ["2년", "3년"]},
    {"q": "기념일 축하제도로 지원되는 금액은 얼마인가요?", "src": "41.기념일",
     "must": ["10만원"], "must_not": ["100만원", "200만원"]},
    {"q": "임직원 1인이 받을 수 있는 주택자금 대출 한도는 얼마인가요?", "src": "21.주택자금",
     "must": ["3천만원|3,000만원"], "must_not": ["100만원", "3백만원"]},
    {"q": "주택자금 대출은 몇 년에 걸쳐 상환하나요?", "src": "21.주택자금",
     "must": ["5년"], "must_not": ["3년에걸쳐", "3년원금", "3년균등", "2년에걸쳐"]},
    {"q": "원격지 근무자에게 침구류는 얼마까지 지원되나요?", "src": "12.원격지",
     "must": ["25만원"], "must_not": ["100만원"]},
    {"q": "출장 여비는 어떤 항목들로 구분되나요?", "src": "25.출장여비",
     "must": ["일비"], "must_not": []},
]

V3_PROMPT = (
    "당신은 사내 규정 검색 도우미입니다. [근거]에는 질문과 무관한 조항이 섞여 있습니다.\n"
    "1) '인용:' 뒤에 질문에 직접 답하는 문장 하나만 [근거]에서 글자 그대로 옮깁니다.\n"
    "2) '답변:' 뒤에 그 인용문만 사용해 딱 한 문장으로 답합니다. 같은 말을 반복하지 마세요.\n"
    "인용에 없는 숫자·날짜·기간·금액은 절대 쓰지 마세요."
)


#------------------------------------------------------------------
# 실문서 추출 -> 청킹
#=> CSOClassify extract 모듈(하이브리드)로 텍스트를 뽑고, e5 토크나이저 기준
#   토큰 수로 잘라 청크를 만든다. 청크마다 출처 파일명을 함께 보관해야
#   검색 정확도를 채점할 수 있다.
#
# -in: tok = 토크나이저, chunk_tokens/overlap
#
# -out: (chunks, sources) = 청크 텍스트 목록, 각 청크의 출처 파일명
#------------------------------------------------------------------
def build_corpus(tok, chunk_tokens, overlap):
    if CSO_SRC not in sys.path:
        sys.path.insert(0, CSO_SRC)
    from csoclassify.extract import build_extractor

    ext = build_extractor(hybrid=True)

    # ⚠️ 필수: e5 의 tokenizer.json 에는 max_length=512 절단 설정이 들어 있다.
    # 해제하지 않고 긴 문서를 encode 하면 앞 512 토큰만 남고 나머지가 조용히
    # 사라진다(취업규칙 11,830토큰 → 512토큰, 96% 유실). 청킹 전에 반드시 끈다.
    tok.no_truncation()

    chunks, sources = [], []

    for name in sorted(os.listdir(DOC_DIR)):
        path = os.path.join(DOC_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            text = ext.extract(path)
        except Exception:
            continue
        if not text:
            continue

        ids = tok.encode(text, add_special_tokens=False).ids
        step = max(1, chunk_tokens - overlap)
        for start in range(0, max(1, len(ids)), step):
            piece = ids[start:start + chunk_tokens]
            if len(piece) < 16:            # 꼬리 조각은 버린다(의미가 거의 없음)
                continue
            chunks.append(tok.decode(piece))
            sources.append(name)
            if start + chunk_tokens >= len(ids):
                break
    return chunks, sources


#------------------------------------------------------------------
# BM25 토크나이징 방식
#=> 한국어는 조사가 붙어 공백 분리만으로는 '휴가비는'과 '휴가비'가 다른 토큰이
#   된다. e5 서브워드로 자르면 조사가 분리돼 매칭이 살아나는지 비교한다.
#   (설계서 R7 대응)
#
# -in: text, tok, mode = "space" | "subword"
#
# -out: 토큰 리스트
#------------------------------------------------------------------
def bm25_tokens(text, tok, mode):
    if mode == "subword":
        return tok.encode(text, add_special_tokens=False).tokens
    return text.split()


#------------------------------------------------------------------
# must 키워드 판정 ("a|b" 는 택일)
#=> 같은 사실을 표기하는 방식이 여러 가지인 경우(2백만원/200만원)를 인정한다.
#------------------------------------------------------------------
def has_must(flat, spec):
    return any(re.sub(r"\s+", "", alt) in flat for alt in spec.split("|"))


#------------------------------------------------------------------
# 1단계: 검색 품질 (핵심 — R1)
#=> 청크 크기별로 인덱스를 만들고, 질문의 정답 문서가 top-k 에 들어오는지 센다.
#   dense/BM25/hybrid 를 나눠 재서 하이브리드의 실익을 확인한다.
#   벡터DB 없이 numpy 로 직접 코사인을 계산한다 — 실코퍼스는 수백 청크라
#   Qdrant 를 띄울 필요가 없고, 검색 '정확도' 측정에는 영향이 없다.
#------------------------------------------------------------------
def stage_retrieval(args):
    from rank_bm25 import BM25Okapi

    emb = BatchOnnxEmbedder(num_threads=args.embed_threads)
    results = []

    for ct in args.chunk_sizes:
        overlap = max(8, ct // 8)
        t0 = time.perf_counter()
        chunks, sources = build_corpus(emb.tok, ct, overlap)
        t_build = time.perf_counter() - t0

        t0 = time.perf_counter()
        vecs = np.vstack([emb.embed(chunks[i:i + 8])
                          for i in range(0, len(chunks), 8)])
        t_embed = time.perf_counter() - t0

        # BM25 를 토크나이징 방식별로 만들어 함께 비교한다(R7).
        bms = {m: BM25Okapi([bm25_tokens(c, emb.tok, m) for c in chunks])
               for m in args.bm25_modes}

        keys = ["dense"] + ["bm25_" + m for m in args.bm25_modes] \
                         + ["hybrid_" + m for m in args.bm25_modes]
        hit = {k: 0 for k in keys}

        for case in CASES:
            qv = emb.embed([case["q"]], prefix=QUERY_PREFIX)[0]
            dense_rank = [int(x) for x in np.argsort(vecs @ qv)[::-1][:args.first_stage]]
            if any(case["src"] in sources[i] for i in dense_rank[:args.top_k]):
                hit["dense"] += 1

            for m in args.bm25_modes:
                qt = bm25_tokens(case["q"], emb.tok, m)
                bm_rank = [int(x) for x in
                           np.argsort(bms[m].get_scores(qt))[::-1][:args.first_stage]]
                fused = [i for i, _ in rrf([dense_rank, bm_rank])][:args.top_k]
                if any(case["src"] in sources[i] for i in bm_rank[:args.top_k]):
                    hit["bm25_" + m] += 1
                if any(case["src"] in sources[i] for i in fused):
                    hit["hybrid_" + m] += 1

        n = len(CASES)
        row = {"chunk_tokens": ct, "overlap": overlap, "n_chunks": len(chunks),
               "build_s": round(t_build, 1), "embed_s": round(t_embed, 1),
               "top_k": args.top_k}
        row.update({("recall_" + k): round(hit[k] / n, 3) for k in keys})
        results.append(row)

        parts = "  ".join("{} {:>4.0f}%".format(k, hit[k] / n * 100) for k in keys)
        print("  청크{:<4} {:>4}개  Recall@{}:  {}".format(
            ct, len(chunks), args.top_k, parts))
    return results


#------------------------------------------------------------------
# 2단계: 답변 품질 + E2E (실제 검색 결과로 생성)
#=> 1단계와 달리 검색을 완벽하다고 가정하지 않는다. 실제 하이브리드 검색이
#   가져온 청크로 답을 만들게 해, 검색 실패까지 포함한 실전 정답률을 본다.
#------------------------------------------------------------------
def stage_answer(args):
    from rank_bm25 import BM25Okapi
    from bench_llm import MODELS, MODELS_DIR, build_prompt, load_llm

    emb = BatchOnnxEmbedder(num_threads=args.embed_threads)
    chunks, sources = build_corpus(emb.tok, args.chunk_tokens,
                                   max(8, args.chunk_tokens // 8))
    vecs = np.vstack([emb.embed(chunks[i:i + 8]) for i in range(0, len(chunks), 8)])
    # 서브워드 토크나이징 — 공백 분리는 한국어 조사 때문에 Recall@1 이 67% 로
    # 떨어졌다(R7). 서브워드로 바꾸면 92~100% 로 회복된다.
    bm = BM25Okapi([bm25_tokens(c, emb.tok, args.bm25_mode) for c in chunks])
    print("  코퍼스: {}청크 (청크 {}토큰, BM25={})\n".format(
        len(chunks), args.chunk_tokens, args.bm25_mode))

    from bench_quality import PROMPTS
    system = dict(PROMPTS, V3_real=V3_PROMPT)[args.variant]
    llm, _ = load_llm(os.path.join(MODELS_DIR, MODELS[args.model]),
                      args.llm_threads, args.n_ctx)
    print("  프롬프트: {} ({}자)\n".format(args.variant, len(system)))

    rows = []
    for case in CASES:
        t0 = time.perf_counter()
        qv = emb.embed([case["q"]], prefix=QUERY_PREFIX)[0]
        dense_rank = [int(i) for i in np.argsort(vecs @ qv)[::-1][:args.first_stage]]
        bm_rank = [int(i) for i in np.argsort(bm.get_scores(
            bm25_tokens(case["q"], emb.tok, args.bm25_mode)))[::-1][:args.first_stage]]
        picked = [i for i, _ in rrf([dense_rank, bm_rank])][:args.top_k]
        t_retr = time.perf_counter() - t0

        retrieved_ok = any(case["src"] in sources[i] for i in picked)
        user = ("[근거]\n" + "\n\n".join(
            "[{}] {}".format(k + 1, chunks[i]) for k, i in enumerate(picked))
            + "\n\n[질문]\n" + case["q"])

        llm.reset()
        t1 = time.perf_counter()
        t_first = None
        out = []
        for ch in llm.create_completion(build_prompt(system, user),
                                        max_tokens=args.max_tokens, temperature=0.0,
                                        stop=["<|im_end|>"], stream=True):
            if t_first is None:
                t_first = time.perf_counter()
            out.append(ch["choices"][0]["text"])
        t_end = time.perf_counter()
        text = "".join(out)

        # 결정7 폴백: 답변: → 인용: → 전체
        if "답변:" in text:
            target = text.split("답변:", 1)[1]
        elif "인용:" in text:
            target = text.split("인용:", 1)[1]
        else:
            target = text
        flat = re.sub(r"\s+", "", target)

        got = [m for m in case["must"] if has_must(flat, m)]
        bad = [m for m in case["must_not"] if re.sub(r"\s+", "", m) in flat]

        rows.append({
            "q": case["q"], "src": case["src"],
            "retrieved_ok": retrieved_ok,
            "correct": len(got) == len(case["must"]) and not bad,
            "confused": bool(bad), "confuse_terms": bad,
            "retrieval_ms": round(t_retr * 1000, 1),
            "ttft_s": round((t_first or t_end) - t1, 2),
            "total_s": round(t_end - t0, 2),
            "sources_top3": [sources[i] for i in picked],
            "answer": text.strip().replace("\n", " ")[:220],
        })
        print("  [{}{}] {:<44} {:.2f}s  {}".format(
            "R" if retrieved_ok else "-", "A" if rows[-1]["correct"] else "-",
            case["q"][:42], rows[-1]["total_s"],
            ",".join(bad) if bad else ""))

    n = len(rows)
    print("\n  검색 성공(정답문서 포함) : {:.0f}%".format(
        sum(r["retrieved_ok"] for r in rows) / n * 100))
    print("  최종 정답률              : {:.0f}%".format(
        sum(r["correct"] for r in rows) / n * 100))
    print("  혼동률                   : {:.0f}%".format(
        sum(r["confused"] for r in rows) / n * 100))
    print("  검색 지연(중앙값)         : {:.0f} ms".format(
        statistics.median(r["retrieval_ms"] for r in rows)))
    print("  TTFT(중앙값)             : {:.2f} s".format(
        statistics.median(r["ttft_s"] for r in rows)))
    return rows


def main():
    p = argparse.ArgumentParser(description="실문서 검증")
    p.add_argument("--stage", choices=["retrieval", "answer"], default="retrieval")
    p.add_argument("--chunk-sizes", nargs="+", type=int, default=[128, 192, 256, 384])
    p.add_argument("--chunk-tokens", type=int, default=128)
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--first-stage", type=int, default=10)
    p.add_argument("--bm25-modes", nargs="+", default=["space", "subword"])
    p.add_argument("--bm25-mode", default="subword")
    p.add_argument("--variant", default="V3_real")
    p.add_argument("--embed-threads", type=int, default=4)
    p.add_argument("--llm-threads", type=int, default=8)
    p.add_argument("--model", default="qwen3-0.6b-q4")
    p.add_argument("--max-tokens", type=int, default=200)
    p.add_argument("--n-ctx", type=int, default=2048)
    args = p.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("[real] stage={}  문서={}\n".format(args.stage, DOC_DIR))

    res = stage_retrieval(args) if args.stage == "retrieval" else stage_answer(args)

    tag = "_{}_{}".format(args.model.replace("qwen3-","").replace("-q4",""), args.variant) if args.stage=="answer" else ""
    out = os.path.join(RESULTS_DIR, "real_" + args.stage + tag + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("\n[real] 저장: " + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
