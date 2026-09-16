#------------------------------------------------------------------
# BM25 인덱스 — numpy 역색인 (설계서 결정6)
#=> 하이브리드 검색의 나머지 절반. 한국어 고유명사·조문번호·금액 같은
#   표층 일치에서 dense 를 보완한다.
#
#   왜 직접 구현했나
#     초기 구현은 rank_bm25(BM25Okapi)를 썼는데, 43,058청크 기준 질의당
#     264ms 로 **검색 지연의 69%** 를 차지했다(dense 는 101ms). 순수 파이썬
#     get_scores() 가 매 질의마다 전체 문서를 훑기 때문이다.
#     scipy 를 들이는 대신 numpy 만으로 역색인을 만들어 해결한다.
#
#   자료구조 (모두 평탄한 numpy 배열 — 캐시 적재가 빠르다)
#     vocab        : 토큰 -> term_id
#     term_offset  : term_id 별 postings 구간 [start, end)
#     post_doc     : postings 의 문서 행번호 (int32)
#     post_w       : postings 의 BM25 가중치 (float32, 미리 계산)
#
#   질의 시에는 등장한 term 의 postings 만 더하면 된다. (term, doc) 쌍은
#   유일하므로 fancy-index 누산이 안전하다.
#
#     scores = zeros(n_docs)
#     for t in query_terms:  scores[post_doc[s:e]] += post_w[s:e]
#
#   ⚠️ 토크나이징은 반드시 '서브워드'다. 공백 분리는 한국어 조사 때문에
#   '휴가비는'과 '휴가비'가 달라져 Recall@1 이 67% 까지 떨어진다(REPORT §13.4).
#------------------------------------------------------------------

import os
import threading
import time

import numpy as np

from .. import config

# BM25 하이퍼파라미터(k1=1.5, b=0.75 — Lucene 관례값)는 config.BM25_K1 / BM25_B 로 옮겼다
#   (config.yaml retrieval.bm25_k1 · bm25_b, REPORT §35). 가중치를 build() 때 미리
#   계산하므로 바꾸면 다음 index 실행부터 반영된다.

# 캐시 포맷 버전. 구조가 바뀌면 올려서 옛 캐시를 무시하게 한다.
CACHE_VERSION = 2


#------------------------------------------------------------------
# BM25 토크나이징
#=> subword 는 e5 토크나이저를 그대로 쓴다. 임베딩과 같은 어휘를 보게 되어
#   두 검색기의 관점이 지나치게 어긋나지 않는 이점도 있다.
#
# -in: text, tok, mode = "subword" | "space"
#
# -out: 토큰 리스트
#------------------------------------------------------------------
def tokenize(text, tok, mode=None):
    mode = mode or config.BM25_MODE
    if mode == "subword":
        return tok.encode(text, add_special_tokens=False).tokens
    return text.split()


class Bm25Index:
    #------------------------------------------------------------------
    # 생성자 — 경로만 보관(인덱스는 아직 로드 안 함)
    #------------------------------------------------------------------
    def __init__(self, path=None):
        self.path = path or config.BM25_PATH
        self.vocab = None          # {token: term_id}
        self.term_offset = None    # int64 (n_terms + 1,)
        self.post_doc = None       # int32 (nnz,)
        self.post_w = None         # float32 (nnz,)
        self.ids = None            # 행번호 -> Qdrant point id
        self._load_lock = threading.Lock()

    def is_ready(self):
        return self.vocab is not None and self.post_doc is not None

    #------------------------------------------------------------------
    # 인덱스 구축 (핵심)
    #=> 문서별 토큰 빈도를 세어 역색인을 만들고, BM25 가중치를 미리 계산해 둔다.
    #   질의 시점에 남는 계산은 '더하기' 뿐이다.
    #
    #     w(d,t) = idf(t) * tf * (k1+1) / (tf + k1*(1 - b + b*dl/avgdl))
    #     idf(t) = ln( (N - df + 0.5) / (df + 0.5) + 1 )    # 항상 양수
    #
    # -in: chunks = 청크 텍스트 목록
    # -in: ids    = 각 청크의 point id (dense 결과와 맞추기 위함)
    # -in: tok    = 토크나이저
    #
    # -out: build_s = 구축 소요 초
    #------------------------------------------------------------------
    def build(self, chunks, ids, tok):
        t0 = time.perf_counter()
        n_docs = len(chunks)

        vocab = {}
        rows_t, rows_d, rows_tf = [], [], []      # (term_id, doc_row, tf)
        doc_len = np.zeros(n_docs, dtype=np.float32)

        for d, text in enumerate(chunks):
            counts = {}
            for tkn in tokenize(text, tok):
                counts[tkn] = counts.get(tkn, 0) + 1
            doc_len[d] = sum(counts.values())
            for tkn, tf in counts.items():
                tid = vocab.get(tkn)
                if tid is None:
                    tid = len(vocab)
                    vocab[tkn] = tid
                rows_t.append(tid)
                rows_d.append(d)
                rows_tf.append(tf)

        n_terms = len(vocab)
        t_arr = np.asarray(rows_t, dtype=np.int64)
        d_arr = np.asarray(rows_d, dtype=np.int32)
        tf_arr = np.asarray(rows_tf, dtype=np.float32)

        # term 별로 정렬해 postings 를 연속 구간으로 만든다.
        order = np.argsort(t_arr, kind="stable")
        t_arr, d_arr, tf_arr = t_arr[order], d_arr[order], tf_arr[order]

        # df[t] = t 가 등장한 문서 수 = postings 길이. 누적합이 곧 구간 경계다.
        df = np.bincount(t_arr, minlength=n_terms).astype(np.float32)
        term_offset = np.zeros(n_terms + 1, dtype=np.int64)
        term_offset[1:] = np.cumsum(df.astype(np.int64))

        avgdl = float(doc_len.mean()) if n_docs else 1.0
        idf = np.log((n_docs - df + 0.5) / (df + 0.5) + 1.0).astype(np.float32)

        # 가중치 사전 계산 — 질의 때는 이 값을 더하기만 한다.
        dl = doc_len[d_arr]
        k1, b = float(config.BM25_K1), float(config.BM25_B)     # config.yaml 에서 온다
        denom = tf_arr + k1 * (1.0 - b + b * dl / max(avgdl, 1e-9))
        post_w = (idf[t_arr] * tf_arr * (k1 + 1.0) / denom).astype(np.float32)

        self.vocab = vocab
        self.term_offset = term_offset
        self.post_doc = d_arr
        self.post_w = post_w
        self.ids = np.asarray(ids, dtype=np.int64)

        self.save()
        return time.perf_counter() - t0

    #------------------------------------------------------------------
    # 디스크 저장 / 적재
    #=> npz 로 배열을 그대로 담는다. pickle 보다 적재가 빠르고 안전하다.
    #   vocab 은 토큰 배열 + term_id 배열로 나눠 저장한다.
    #------------------------------------------------------------------
    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        terms = np.array(list(self.vocab.keys()), dtype=object)
        tids = np.asarray(list(self.vocab.values()), dtype=np.int64)
        np.savez(self.path, version=np.int32(CACHE_VERSION),
                 terms=terms, tids=tids, term_offset=self.term_offset,
                 post_doc=self.post_doc, post_w=self.post_w, ids=self.ids)

    #------------------------------------------------------------------
    # 캐시에서 적재 보장(지연 로딩)
    #=> 캐시가 없거나 버전이 다르면 준비되지 않은 상태로 둔다
    #   (하이브리드가 dense 단독으로 자동 폴백한다).
    #
    # -out: load_ms = 로딩 밀리초. 캐시 없음/불일치면 -1.0
    #------------------------------------------------------------------
    def ensure_loaded(self):
        if self.is_ready():
            return 0.0

        with self._load_lock:
            if self.is_ready():
                return 0.0

            path = self.path if os.path.isfile(self.path) else self.path + ".npz"
            if not os.path.isfile(path):
                return -1.0

            t0 = time.perf_counter()
            try:
                z = np.load(path, allow_pickle=True)
                if int(z["version"]) != CACHE_VERSION:
                    return -1.0
                terms = z["terms"]
                tids = z["tids"]
                self.vocab = {str(t): int(i) for t, i in zip(terms, tids)}
                self.term_offset = z["term_offset"]
                self.post_doc = z["post_doc"]
                self.post_w = z["post_w"]
                self.ids = z["ids"]
            except Exception:
                self.vocab = None          # 깨진 캐시는 없는 것으로 취급
                self.post_doc = None
                return -1.0
            return (time.perf_counter() - t0) * 1000.0

    #------------------------------------------------------------------
    # 검색 (핵심)
    #=> 질의 토큰의 postings 만 누산한다. 전체 문서를 훑지 않는다.
    #
    # -in: query, tok, limit
    #
    # -out: [point_id, ...] 점수 순. 인덱스가 없으면 []
    #------------------------------------------------------------------
    def search(self, query, tok, limit):
        if self.ensure_loaded() < 0 or not self.is_ready():
            return []

        n_docs = len(self.ids)
        scores = np.zeros(n_docs, dtype=np.float32)

        seen = set()
        for tkn in tokenize(query, tok):
            if tkn in seen:                 # 같은 토큰을 두 번 더하지 않는다
                continue
            seen.add(tkn)
            tid = self.vocab.get(tkn)
            if tid is None:
                continue
            s, e = int(self.term_offset[tid]), int(self.term_offset[tid + 1])
            if e > s:
                # (term, doc) 쌍은 유일하므로 fancy-index 누산이 안전하다.
                scores[self.post_doc[s:e]] += self.post_w[s:e]

        if not np.any(scores):
            return []

        k = min(limit, n_docs)
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [int(self.ids[i]) for i in top if scores[i] > 0]
