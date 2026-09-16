#------------------------------------------------------------------
# 임베딩 + 벡터검색 단계 속도 실측
#=> LLM 만 재서는 RAG 전체 예산을 못 짠다. 이 벤치는 나머지 절반을 잰다.
#    1) 임베딩 처리량 — 배치 크기별 (인덱싱 시간을 좌우)
#    2) 5만 청크 실제 인덱싱 — 임베딩 + Qdrant 적재
#    3) 질의 지연 — 쿼리 임베딩 / 벡터검색 / BM25 / 하이브리드(RRF)
#
#   모델은 CSOClassify 가 이미 만들어 둔 ONNX int8 산출물을 그대로 쓴다
#   (dragonkue/multilingual-e5-small-ko, 384차원, 118MB).
#
#   주의: CSOClassify 의 OnnxEmbedder 는 청크를 1개씩 추론한다(_embed_one).
#   여기서는 배치 추론을 구현해 그 차이가 얼마나 되는지도 함께 본다.
#
#   사용:
#     python bench/bench_retrieval.py --stage batch     # 배치 크기 스윕
#     python bench/bench_retrieval.py --stage index     # 5만 청크 인덱싱 + 질의
#------------------------------------------------------------------

import argparse
import json
import os
import shutil
import statistics
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.abspath(os.path.join(_HERE, "..", "results"))
QDRANT_DIR = os.path.abspath(os.path.join(_HERE, "..", "qdrant_data"))

# CSOClassify 가 만들어 둔 ONNX int8 임베딩 모델을 재사용한다.
MODEL_DIR = r"D:\Project\CSOClassify\resources\models\e5-small-ko"
DIM = 384
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "

# 청크 본문 생성용 한국어 문장 풀 — 사내규정 문서와 비슷한 밀도.
_POOL = [
    "회사는 1년간 80퍼센트 이상 출근한 직원에게 15일의 유급휴가를 부여한다.",
    "계속 근로기간이 1년 미만인 직원에게는 1개월 개근 시 1일의 유급휴가를 부여한다.",
    "3년 이상 계속 근로한 직원에게는 최초 1년을 초과하는 매 2년마다 1일을 가산한다.",
    "가산휴가를 포함한 총 휴가일수는 25일을 한도로 한다.",
    "연차유급휴가는 1년간 행사하지 아니하면 소멸한다.",
    "미사용 연차유급휴가에 대하여는 통상임금을 기준으로 수당을 지급한다.",
    "연차수당은 휴가청구권이 소멸한 날이 속하는 달의 다음 급여지급일에 지급한다.",
    "사용촉진 조치를 적법하게 이행한 경우 미사용 휴가 보상 의무를 면한다.",
    "사용촉진은 소멸 6개월 전을 기준으로 미사용 일수를 서면 통보하는 절차를 포함한다.",
    "출산전후휴가 및 육아휴직 기간은 연차휴가 산정 시 출근한 것으로 본다.",
    "퇴직 시에는 미사용 연차일수 전부에 대하여 수당을 정산하여 지급한다.",
    "연차휴가의 산정 기준일은 매년 1월 1일이며 회계연도 기준으로 관리한다.",
    "직원은 소정근로시간을 준수하여야 하며 지각·조퇴는 사전에 승인받아야 한다.",
    "시간외근로는 부서장의 사전 승인을 받은 경우에만 인정한다.",
    "재택근무는 주 2일을 한도로 하며 부서 사정에 따라 조정할 수 있다.",
    "출장비는 실비 정산을 원칙으로 하며 영수증 제출이 필요하다.",
    "교육훈련비는 연간 한도 내에서 부서 예산으로 집행한다.",
    "경조사 휴가는 관련 증빙을 제출한 경우에 한하여 부여한다.",
    "회사 자산의 반출은 자산관리 담당자의 승인을 받아야 한다.",
    "보안 사고 발생 시 즉시 정보보호 담당 부서에 신고하여야 한다.",
]


#------------------------------------------------------------------
# ONNX 임베더 (배치 추론 지원)
#=> CSOClassify 의 OnnxEmbedder 와 같은 자산(model.onnx/tokenizer.json)을 쓰되,
#   여러 청크를 한 번에 밀어 넣는 배치 경로를 추가했다. 배치는 행렬곱을 키워
#   BLAS 효율을 올리므로 CPU 에서 특히 이득이 크다.
#------------------------------------------------------------------
class BatchOnnxEmbedder:
    def __init__(self, model_dir=MODEL_DIR, num_threads=None, max_len=512,
                 model_file="model.onnx"):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.max_len = max_len
        self.model_file = model_file
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if num_threads:
            so.intra_op_num_threads = int(num_threads)

        t0 = time.perf_counter()
        self.sess = ort.InferenceSession(
            os.path.join(model_dir, model_file),
            sess_options=so, providers=["CPUExecutionProvider"])
        self.load_s = time.perf_counter() - t0
        self.input_names = {i.name for i in self.sess.get_inputs()}

    #------------------------------------------------------------------
    # 텍스트 목록 -> 벡터 행렬 (핵심)
    #=> 배치 안에서 가장 긴 시퀀스에 맞춰 패딩하고 한 번에 추론한다.
    #   마스크 가중 평균 풀링 후 L2 정규화 (e5 계열 규약).
    #
    # -in: texts  = 원문 목록(프리픽스 미포함)
    # -in: prefix = 'passage: ' 또는 'query: '
    #
    # -out: vecs = shape (len(texts), 384) float32, L2 정규화됨
    #------------------------------------------------------------------
    def embed(self, texts, prefix=PASSAGE_PREFIX):
        encs = [self.tok.encode(prefix + t, add_special_tokens=True) for t in texts]
        lens = [min(len(e.ids), self.max_len) for e in encs]
        width = max(lens)

        ids = np.zeros((len(encs), width), dtype=np.int64)
        attn = np.zeros((len(encs), width), dtype=np.int64)
        for i, e in enumerate(encs):
            n = lens[i]
            ids[i, :n] = e.ids[:n]
            attn[i, :n] = e.attention_mask[:n]

        feeds = {}
        if "input_ids" in self.input_names:
            feeds["input_ids"] = ids
        if "attention_mask" in self.input_names:
            feeds["attention_mask"] = attn
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)

        hidden = self.sess.run(None, feeds)[0]          # (B, T, 384)

        # 마스크 가중 평균 — 패딩 자리는 빼고 평균낸다.
        m = attn.astype(np.float32)[:, :, None]
        summed = (hidden * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        vecs = summed / counts

        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / np.clip(norms, 1e-9, None)).astype(np.float32)


#------------------------------------------------------------------
# 합성 청크 생성
#=> 목표 토큰 수에 맞춰 문장을 이어 붙이되, 청크마다 시작 위치와 고유 번호를
#   달리해 서로 다른 문서가 되게 한다(전부 같으면 검색 측정이 의미 없어진다).
#
# -in: n, target_tokens, tok
#
# -out: chunks = 텍스트 리스트
#------------------------------------------------------------------
def make_corpus(n, target_tokens, tok):
    # 문장 하나당 토큰 수를 미리 재서, 매 청크마다 토크나이즈하지 않게 한다.
    sent_tokens = [len(tok.encode(s, add_special_tokens=False).ids) for s in _POOL]
    avg = sum(sent_tokens) / len(sent_tokens)
    per_chunk = max(1, int(target_tokens / avg))

    chunks = []
    for i in range(n):
        parts = ["[문서{}-{}]".format(i // 20 + 1, i % 20 + 1)]
        for k in range(per_chunk):
            parts.append(_POOL[(i * 7 + k * 3) % len(_POOL)])
        chunks.append(" ".join(parts))
    return chunks


#------------------------------------------------------------------
# 1단계: 배치 크기 스윕
#=> 배치를 키우면 행렬곱이 커져 CPU 효율이 오른다. 어디서 포화되는지 찾는다.
#   batch=1 은 CSOClassify 현재 방식과 같아 '개선 여지' 의 기준선이 된다.
#------------------------------------------------------------------
def stage_batch(args):
    results = []
    # int8 양자화 모델과 fp32 원본을 함께 잰다.
    #   이 CPU 는 AVX-512(VNNI)가 없어 int8 GEMM 가속을 못 받는다. ONNX 동적
    #   양자화는 quantize/dequantize 오버헤드까지 더하므로, VNNI 없는 CPU 에서는
    #   int8 이 fp32 보다 느려지는 일이 흔하다 — 실제로 그런지 확인한다.
    for model_file in args.model_files:
        path = os.path.join(MODEL_DIR, model_file)
        if not os.path.isfile(path):
            print("  [skip] 없음: {}".format(model_file))
            continue

        for nt in args.thread_list:
            emb = BatchOnnxEmbedder(num_threads=nt, model_file=model_file)
            corpus = make_corpus(args.sample, args.chunk_tokens, emb.tok)
            ntok = len(emb.tok.encode(PASSAGE_PREFIX + corpus[0],
                                      add_special_tokens=True).ids)
            size_mb = os.path.getsize(path) / 1024 / 1024

            for bs in args.batches:
                emb.embed(corpus[:bs])                     # 예열
                t0 = time.perf_counter()
                done = 0
                for i in range(0, len(corpus), bs):
                    emb.embed(corpus[i:i + bs])
                    done += len(corpus[i:i + bs])
                dt = time.perf_counter() - t0
                tps = done / dt
                results.append({"model_file": model_file, "size_mb": round(size_mb, 1),
                                "threads": nt, "batch": bs,
                                "chunks_per_s": round(tps, 1),
                                "eta_50k_s": round(50000 / tps, 1),
                                "chunk_tokens": ntok})
                print("  {:<18} threads={:<3} batch={:<4} {:>7.1f} chunk/s   "
                      "5만건 {:>6.0f}s".format(model_file, nt, bs, tps, 50000 / tps))
            del emb
    return results


#------------------------------------------------------------------
# 2단계: 5만 청크 실제 인덱싱 + 질의 지연 측정
#=> 추정이 아니라 실제로 5만 개를 임베딩하고 Qdrant local 모드에 넣은 뒤,
#   질의 한 건이 실제로 몇 ms 걸리는지 잰다.
#------------------------------------------------------------------
def stage_index(args):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct

    emb = BatchOnnxEmbedder(num_threads=args.threads, model_file=args.model_file)
    corpus = make_corpus(args.n, args.chunk_tokens, emb.tok)
    ntok = len(emb.tok.encode(PASSAGE_PREFIX + corpus[0], add_special_tokens=True).ids)
    print("  코퍼스: {:,}청크 x {}토큰".format(len(corpus), ntok))

    # (1) 임베딩 — 전체 실측
    bs = args.best_batch
    t0 = time.perf_counter()
    vecs = np.empty((len(corpus), DIM), dtype=np.float32)
    for i in range(0, len(corpus), bs):
        vecs[i:i + bs] = emb.embed(corpus[i:i + bs])
        if (i // bs) % 100 == 0 and i:
            el = time.perf_counter() - t0
            print("    ... {:,}/{:,}  {:.0f} chunk/s".format(i, len(corpus), i / el))
    embed_s = time.perf_counter() - t0
    print("  [1] 임베딩 : {:.1f}s  ({:.0f} chunk/s)".format(embed_s, len(corpus) / embed_s))

    # (2) Qdrant local 모드 적재
    if os.path.isdir(QDRANT_DIR):
        shutil.rmtree(QDRANT_DIR, ignore_errors=True)
    client = QdrantClient(path=QDRANT_DIR)
    client.create_collection(
        collection_name="rag",
        vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))

    t0 = time.perf_counter()
    UP = 1000
    for i in range(0, len(corpus), UP):
        pts = [PointStruct(id=i + j, vector=vecs[i + j].tolist(),
                           payload={"text": corpus[i + j]})
               for j in range(len(corpus[i:i + UP]))]
        client.upsert(collection_name="rag", points=pts)
    upsert_s = time.perf_counter() - t0
    print("  [2] Qdrant 적재: {:.1f}s".format(upsert_s))

    # (3) 질의 지연 — 쿼리 임베딩 / 벡터검색
    queries = ["연차휴가 미사용 수당은 언제 지급되나요?",
               "재택근무는 며칠까지 가능한가요?",
               "출장비 정산 기준을 알려주세요.",
               "보안 사고가 나면 어디에 신고하나요?",
               "사용촉진 절차는 어떻게 되나요?"]

    q_embed_ms, q_search_ms = [], []
    for _ in range(args.repeat):
        for q in queries:
            t0 = time.perf_counter()
            qv = emb.embed([q], prefix=QUERY_PREFIX)[0]
            t1 = time.perf_counter()
            client.query_points(collection_name="rag", query=qv.tolist(),
                                limit=args.top_k, with_payload=True)
            t2 = time.perf_counter()
            q_embed_ms.append((t1 - t0) * 1000)
            q_search_ms.append((t2 - t1) * 1000)

    med_e = statistics.median(q_embed_ms)
    med_s = statistics.median(q_search_ms)
    print("  [3] 질의 지연  : 임베딩 {:.1f}ms + 검색 {:.1f}ms = {:.1f}ms".format(
        med_e, med_s, med_e + med_s))

    # (4) BM25 (하이브리드 검색의 나머지 절반)
    bm25_ms = None
    try:
        from rank_bm25 import BM25Okapi
        t0 = time.perf_counter()
        bm = BM25Okapi([c.split() for c in corpus])
        bm25_build_s = time.perf_counter() - t0
        lat = []
        for _ in range(args.repeat):
            for q in queries:
                t0 = time.perf_counter()
                bm.get_top_n(q.split(), corpus, n=args.top_k)
                lat.append((time.perf_counter() - t0) * 1000)
        bm25_ms = statistics.median(lat)
        print("  [4] BM25       : 인덱스 구축 {:.1f}s / 질의 {:.0f}ms".format(
            bm25_build_s, bm25_ms))
    except ImportError:
        bm25_build_s = None
        print("  [4] BM25       : rank_bm25 미설치 — 건너뜀")

    size_mb = sum(os.path.getsize(os.path.join(r, f))
                  for r, _, fs in os.walk(QDRANT_DIR) for f in fs) / 1024 / 1024

    return [{
        "n_chunks": len(corpus), "chunk_tokens": ntok, "batch": bs,
        "embed_s": round(embed_s, 1),
        "embed_chunks_per_s": round(len(corpus) / embed_s, 1),
        "qdrant_upsert_s": round(upsert_s, 1),
        "qdrant_disk_mb": round(size_mb, 1),
        "query_embed_ms": round(med_e, 1),
        "query_search_ms": round(med_s, 1),
        "query_total_ms": round(med_e + med_s, 1),
        "bm25_build_s": round(bm25_build_s, 1) if bm25_build_s else None,
        "bm25_query_ms": round(bm25_ms, 1) if bm25_ms else None,
        "top_k": args.top_k,
    }]


def main():
    p = argparse.ArgumentParser(description="임베딩/검색 속도 실측")
    p.add_argument("--stage", choices=["batch", "index"], default="batch")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--thread-list", nargs="+", type=int, default=[4, 8])
    p.add_argument("--model-files", nargs="+",
                   default=["model.onnx", "model.fp32.onnx"])
    p.add_argument("--model-file", default="model.onnx",
                   help="index 단계에서 쓸 모델 파일")
    p.add_argument("--batches", nargs="+", type=int, default=[1, 4, 8, 16, 32, 64])
    p.add_argument("--sample", type=int, default=1000)
    p.add_argument("--n", type=int, default=50000)
    p.add_argument("--chunk-tokens", type=int, default=128)
    p.add_argument("--best-batch", type=int, default=32)
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--repeat", type=int, default=5)
    args = p.parse_args()

    if not os.path.isdir(MODEL_DIR):
        print("[error] 임베딩 모델 폴더 없음: " + MODEL_DIR, file=sys.stderr)
        return 2

    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("[retrieval] stage={} threads={}\n".format(args.stage, args.threads))

    results = stage_batch(args) if args.stage == "batch" else stage_index(args)

    out = os.path.join(RESULTS_DIR, "retrieval_" + args.stage + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n[retrieval] 저장: " + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
