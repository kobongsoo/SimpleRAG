#------------------------------------------------------------------
# BM25 인덱스 — numpy 역색인, 세그먼트형 (설계서 결정6 · 인덱싱 속도 개선 설계서 §4)
#=> 하이브리드 검색의 나머지 절반. 한국어 고유명사·조문번호·금액 같은
#   표층 일치에서 dense 를 보완한다.
#
#   왜 직접 구현했나
#     초기 구현은 rank_bm25(BM25Okapi)를 썼는데, 43,058청크 기준 질의당
#     264ms 로 **검색 지연의 69%** 를 차지했다(dense 는 101ms). 순수 파이썬
#     get_scores() 가 매 질의마다 전체 문서를 훑기 때문이다.
#     scipy 를 들이는 대신 numpy 만으로 역색인을 만들어 해결한다.
#
#   왜 세그먼트형인가 (2026-10-30)
#     예전에는 가중치를 미리 계산해 저장했다. 가중치에는 전체 통계(N · 평균 길이 · df)가
#     들어가므로 문서 1건만 바뀌어도 전체를 다시 만들어야 했다(운영 18.7만 청크 36초).
#     지금은 단어 빈도(tf)만 저장하고 가중치는 질의 때 계산한다.
#       본 세그먼트   : 전체 인덱싱 때 만든 역색인
#       덧붙임 세그먼트: 그 뒤 새로 넣은 청크(작다)
#       지움 표시     : 본 세그먼트에서 지운 청크
#     문서 추가·삭제는 덧붙임 · 지움만 고친다(ms 단위). 둘 중 하나가 본의 5% 를 넘으면 합친다.
#     df 를 지운 청크를 빼고 세므로 점수는 전체 재구축과 같다.
#
#   자료구조 (모두 평탄한 numpy 배열 — 캐시 적재가 빠르다)
#     vocab        : 토큰 -> term_id
#     term_offset  : term_id 별 postings 구간 [start, end)
#     post_row     : postings 의 청크 행번호 (int32)
#     post_tf      : postings 의 단어 빈도 (float32)
#     doc_len      : 행별 청크 길이 (float32)
#
#     질의: w = idf * tf * (k1+1) / (tf + k1*(1 - b + b*dl/avgdl)),  idf = ln((N-df+.5)/(df+.5) + 1)
#
#   ⚠️ 토크나이징은 반드시 '서브워드'다. 공백 분리는 한국어 조사 때문에
#   '휴가비는'과 '휴가비'가 달라져 Recall@1 이 67% 까지 떨어진다(REPORT §13.4).
#------------------------------------------------------------------

import os
import sqlite3
import struct
import threading
import time

import numpy as np

from .. import config

# BM25 하이퍼파라미터(k1=1.5, b=0.75 — Lucene 관례값)는 config.BM25_K1 / BM25_B 로 옮겼다
#   (config.yaml retrieval.bm25_k1 · bm25_b, REPORT §35). 가중치를 질의 때 계산하므로
#   바꾸면 다음 질문부터 반영된다.

# 캐시 포맷 버전. 구조가 바뀌면 올려서 옛 캐시를 무시하게 한다(2 = 가중치 미리 계산, 3 = 세그먼트형)
CACHE_VERSION = 3

# 덧붙임 · 지움이 본 세그먼트의 이 비율을 넘으면 합친다(전체 다시 만들기)
MERGE_RATIO = 0.05


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


#------------------------------------------------------------------
# 토큰 목록 → 단어별 빈도 (첫 등장 순서)
#
# -in: tokens = 토큰 리스트
#
# -out: [(단어, 빈도)] — 첫 등장 순서
# -out: error = 없음
#------------------------------------------------------------------
def _counts(tokens):
    counts = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    return list(counts.items())


#------------------------------------------------------------------
# 여러 청크의 단어별 빈도 (BM25 단계 A — 토큰화 병렬)
#=> subword 모드는 tokenizers 의 encode_batch 로 한 번에 넘긴다 — Rust 안에서 여러 스레드로
#   나눠 토큰화하고 GIL 도 놓는다. 결과 순서는 입력 순서 그대로라 한 줄씩 할 때와 같다.
#
# -in: texts = 청크 본문 목록
# -in: tok   = e5 토크나이저
#
# -out: 청크마다 [(단어, 빈도)]
# -out: error = 토크나이저 예외 전파
#------------------------------------------------------------------
def term_freqs(texts, tok):
    if config.BM25_MODE != "subword":
        return [_counts(t.split()) for t in texts]
    out = []
    # 한 번에 너무 많이 넘기면 메모리가 커진다 — 2만 개씩
    for i in range(0, len(texts), 20000):
        encs = tok.encode_batch(list(texts[i:i + 20000]), add_special_tokens=False)
        # ⚠️ e5 tokenizer.json 에 padding 설정이 들어 있어 묶음 토큰화는 가장 긴 글에 맞춰 <pad> 를 채운다.
        #   채운 칸(attention mask 0)은 빼야 한 건씩 토큰화한 것(tokenize)과 같아진다
        out.extend(_counts([t for t, m in zip(e.tokens, e.attention_mask) if m]) for e in encs)
    return out


#------------------------------------------------------------------
# 단어 빈도 → 바이트 (토큰 표에 넣는 모양 — Rust bm25::tf_to_blob 과 같다)
#=> [단어 수 u32] 다음 단어마다 [글자 길이 u16][utf-8 글자][빈도 u32]
#
# -in: tf = [(단어, 빈도)]
#
# -out: 바이트
# -out: error = 없음
#------------------------------------------------------------------
def tf_to_blob(tf):
    parts = [struct.pack("<I", len(tf))]
    for t, n in tf:
        s = t.encode("utf-8")[:65535]
        parts.append(struct.pack("<H", len(s)) + s + struct.pack("<I", n))
    return b"".join(parts)


#------------------------------------------------------------------
# 바이트 → 단어 빈도 (tf_to_blob 의 반대)
#
# -in: b = 바이트
#
# -out: [(단어, 빈도)] — 깨졌으면 None
# -out: error = 없음 (깨진 바이트는 None)
#------------------------------------------------------------------
def tf_from_blob(b):
    try:
        (n,), p = struct.unpack_from("<I", b, 0), 4
        out = []
        for _ in range(n):
            (ln,) = struct.unpack_from("<H", b, p)
            p += 2
            s = b[p:p + ln].decode("utf-8")
            p += ln
            (c,) = struct.unpack_from("<I", b, p)
            p += 4
            out.append((s, c))
        return out
    except (struct.error, UnicodeDecodeError):
        return None


#------------------------------------------------------------------
# 청크별 단어 빈도 표 (BM25 단계 B — 설계서 §4-3)
#=> Python 판 벡터 저장소(Qdrant local)에는 표를 더 둘 수 없어 BM25 캐시 옆 sqlite 한 파일에 둔다.
#   BM25 를 다시 만들 때 토큰화(전체의 약 60%)를 건너뛴다. id 는 --rebuild 전에는 다시 쓰지 않으므로
#   지운 청크의 줄이 남아도 틀리지 않는다(전체 구축 때 prune 으로 정리).
#
# -필드: path = sqlite 파일 경로 — 표 bm25_tf(id, terms BLOB)
#------------------------------------------------------------------
class TfStore:
    #------------------------------------------------------------------
    # 생성자 — 경로만 보관(파일은 쓸 때 연다)
    #
    # -in: path = sqlite 파일(None 이면 BM25 캐시 옆 bm25_tf.sqlite)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, path=None):
        self.path = path or os.path.join(os.path.dirname(config.BM25_PATH) or ".", "bm25_tf.sqlite")

    #------------------------------------------------------------------
    # 연결 열기
    #=> 쓸 때마다 열고 닫는다 — 워커 · 명령줄이 번갈아 써도 잠금이 남지 않게. 표가 없으면 만든다.
    #
    # -in: 없음
    #
    # -out: sqlite3 연결
    # -out: error = 파일을 열 수 없으면 sqlite3/OSError 전파
    #------------------------------------------------------------------
    def _conn(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        c = sqlite3.connect(self.path)
        c.execute("CREATE TABLE IF NOT EXISTS bm25_tf (id INTEGER PRIMARY KEY, terms BLOB NOT NULL)")
        return c

    #------------------------------------------------------------------
    # 넣기 (같은 id 는 바꾼다)
    #
    # -in: ids = 점 id 목록
    # -in: tfs = 같은 순서의 [(단어, 빈도)] 목록
    #
    # -out: 없음
    # -out: error = sqlite 예외 전파
    #------------------------------------------------------------------
    def put(self, ids, tfs):
        if not ids:
            return
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO bm25_tf (id, terms) VALUES (?, ?)",
                          [(int(i), tf_to_blob(t)) for i, t in zip(ids, tfs)])

    #------------------------------------------------------------------
    # 읽기
    #
    # -in: ids = 점 id 목록(None 이면 표 전체)
    #
    # -out: {id: [(단어, 빈도)]} — 없는 id · 깨진 줄은 빠진다
    # -out: error = sqlite 예외 전파
    #------------------------------------------------------------------
    def get(self, ids=None):
        out = {}
        with self._conn() as c:
            if ids is None:
                rows = c.execute("SELECT id, terms FROM bm25_tf")
            else:
                rows = []
                for i in range(0, len(ids), 900):     # sqlite 인자 수 한도
                    part = [int(x) for x in ids[i:i + 900]]
                    rows.extend(c.execute("SELECT id, terms FROM bm25_tf WHERE id IN ({})".format(
                        ",".join("?" * len(part))), part))
            for i, b in rows:
                tf = tf_from_blob(b)
                if tf is not None:
                    out[int(i)] = tf
        return out

    #------------------------------------------------------------------
    # 비우기 (--rebuild — id 를 0 부터 다시 쓰므로 옛 줄이 섞이면 안 된다)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = sqlite 예외 전파
    #------------------------------------------------------------------
    def clear(self):
        with self._conn() as c:
            c.execute("DELETE FROM bm25_tf")

    #------------------------------------------------------------------
    # 살아 있는 id 말고 모두 지우기 (전체 구축 뒤 찌꺼기 정리)
    #
    # -in: live_ids = 저장소에 있는 점 id 목록
    #
    # -out: 지운 줄 수
    # -out: error = sqlite 예외 전파
    #------------------------------------------------------------------
    def prune(self, live_ids):
        with self._conn() as c:
            # 임시 표에 살아 있는 id 를 넣고 그 밖을 한 번에 지운다(IN 목록 인자 수 한도를 피한다)
            c.execute("CREATE TEMP TABLE live (id INTEGER PRIMARY KEY)")
            c.executemany("INSERT INTO live VALUES (?)", [(int(i),) for i in live_ids])
            n = c.execute("DELETE FROM bm25_tf WHERE id NOT IN (SELECT id FROM live)").rowcount
        return n


#------------------------------------------------------------------
# 단어 빈도들로 세그먼트 만들기 (핵심)
#=> 예전 build() 와 같은 순서 — 청크 차례로, 청크 안 첫 등장 순서로 단어 번호를 매기고
#   단어 번호로 안정 정렬해 postings 를 연속 구간으로 만든다.
#
# -in: tfs = 청크마다 [(단어, 빈도)]
#
# -out: dict {vocab, term_offset, post_row, post_tf, doc_len}
# -out: error = 없음
#------------------------------------------------------------------
def _make_seg(tfs):
    vocab = {}
    rows_t, rows_d, rows_tf = [], [], []
    doc_len = np.zeros(len(tfs), dtype=np.float32)
    for d, tf in enumerate(tfs):
        doc_len[d] = sum(n for _, n in tf)
        for tkn, n in tf:
            tid = vocab.get(tkn)
            if tid is None:
                tid = len(vocab)
                vocab[tkn] = tid
            rows_t.append(tid)
            rows_d.append(d)
            rows_tf.append(n)
    t_arr = np.asarray(rows_t, dtype=np.int64)
    order = np.argsort(t_arr, kind="stable")
    t_arr = t_arr[order]
    term_offset = np.zeros(len(vocab) + 1, dtype=np.int64)
    term_offset[1:] = np.cumsum(np.bincount(t_arr, minlength=len(vocab)))
    return {"vocab": vocab, "term_offset": term_offset,
            "post_row": np.asarray(rows_d, dtype=np.int32)[order],
            "post_tf": np.asarray(rows_tf, dtype=np.float32)[order],
            "doc_len": doc_len}


#------------------------------------------------------------------
# 세그먼트형 BM25 색인 (지연 적재)
# -필드: base    = 본 세그먼트 {vocab, term_offset, post_row, post_tf, doc_len, ids}
# -필드: add     = 덧붙임 세그먼트(같은 모양) / add_tf = 덧붙임 [(id, tf)] id 순
# -필드: deleted = 본 세그먼트 행별 지움 표시 / stamp = 본 표식(덧붙임 파일 짝 확인)
#------------------------------------------------------------------
class Bm25Index:
    #------------------------------------------------------------------
    # 생성자 — 경로만 보관(인덱스는 아직 로드 안 함)
    #
    # -in: path = 본 캐시 파일(None 이면 config.BM25_PATH)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, path=None):
        self.path = path or config.BM25_PATH
        self.base = None           # 본 세그먼트 dict (+ ids)
        self.add = None            # 덧붙임 세그먼트 dict (+ ids)
        self.add_tf = []           # 덧붙임 [(id, tf)] — id 순
        self.deleted = None        # 본 세그먼트 행별 지움 표시 (bool)
        self.stamp = 0             # 본 세그먼트 표식(덧붙임 파일이 같은 본인지)
        self.ids = None            # 옛 코드 호환 — 본 세그먼트 행번호 -> 점 id
        self._n_live = 0
        self._len_sum = 0.0
        self._load_lock = threading.Lock()

    #------------------------------------------------------------------
    # 적재됐나
    #
    # -out: True = 본 세그먼트가 메모리에 있다
    # -out: error = 없음
    #------------------------------------------------------------------
    def is_ready(self):
        return self.base is not None

    #------------------------------------------------------------------
    # 덧붙임 · 지움 파일 경로 (bm25_index.npz → bm25_index.delta.npz)
    #
    # -out: 경로
    # -out: error = 없음
    #------------------------------------------------------------------
    def _delta_path(self):
        base = self.path[:-4] if self.path.endswith(".npz") else self.path
        return base + ".delta.npz"

    #------------------------------------------------------------------
    # 본 파일 경로 (np.savez 는 이름에 .npz 를 붙인다)
    #
    # -out: 경로
    # -out: error = 없음
    #------------------------------------------------------------------
    def _base_file(self):
        return self.path if self.path.endswith(".npz") else self.path + ".npz"

    #------------------------------------------------------------------
    # 덧붙임 세그먼트 · 통계 다시 세기
    #=> 덧붙임은 작아서 통째로 다시 만들어도 ms 단위다.
    #    1) add_tf 를 id 순으로 정렬해 덧붙임 세그먼트를 만든다
    #    2) 살아 있는 청크 수(N)와 길이 합을 센다(평균 길이 = 길이 합 / N)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def _rebuild_add(self):
        self.add_tf.sort(key=lambda x: x[0])
        self.add = _make_seg([t for _, t in self.add_tf])
        self.add["ids"] = np.asarray([i for i, _ in self.add_tf], dtype=np.int64)
        live = ~self.deleted
        self._n_live = int(live.sum()) + len(self.add_tf)
        # 길이 합은 정수 합이라 float64 로 정확히 센다 — 전체 재구축과 같은 평균 길이
        self._len_sum = float(self.base["doc_len"][live].astype(np.float64).sum()
                              + self.add["doc_len"].astype(np.float64).sum())

    #------------------------------------------------------------------
    # 인덱스 구축 — 본문에서(토큰화 포함)
    #=> 토큰화는 encode_batch 로 병렬(단계 A). 업로드 문서처럼 토큰 표가 없는 곳이 쓴다.
    #
    # -in: chunks = 청크 텍스트 목록
    # -in: ids    = 각 청크의 point id (dense 결과와 맞추기 위함)
    # -in: tok    = 토크나이저
    #
    # -out: build_s = 구축 소요 초
    #------------------------------------------------------------------
    def build(self, chunks, ids, tok):
        t0 = time.perf_counter()
        self.build_tf(ids, term_freqs(chunks, tok))
        return time.perf_counter() - t0

    #------------------------------------------------------------------
    # 인덱스 구축 — 단어 빈도에서(전체 다시 만들기 · 합치기)
    #=> 본 세그먼트를 새로 만들고 덧붙임 · 지움은 비운다.
    #
    # -in: ids = 점 id 목록
    # -in: tfs = 같은 순서의 [(단어, 빈도)]
    #
    # -out: build_s = 구축 소요 초
    # -out: error = 파일 저장 실패 시 OSError 전파
    #------------------------------------------------------------------
    def build_tf(self, ids, tfs):
        t0 = time.perf_counter()
        base = _make_seg(tfs)
        base["ids"] = np.asarray(ids, dtype=np.int64)
        self.stamp = time.time_ns()
        self.base = base
        self.ids = base["ids"]
        self.deleted = np.zeros(len(ids), dtype=bool)
        self.add_tf = []
        self._rebuild_add()
        self._save_base()
        # 옛 덧붙임 파일은 새 본에 다 들어갔다(남아도 표식이 달라 무시되지만 지워 둔다)
        try:
            os.remove(self._delta_path())
        except OSError:
            pass
        return time.perf_counter() - t0

    #------------------------------------------------------------------
    # 무엇을 고쳐야 하나 — (넣을 id, 뺄 id)
    #=> 저장소에 지금 있는 점과 색인에 든 점을 맞대어 본다. id 는 다시 쓰지 않으므로 id 만 보면 된다.
    #
    # -in: live_ids = 저장소의 점 id 목록
    #
    # -out: (넣을 id 정렬 목록, 뺄 id 정렬 목록) / 색인을 못 읽으면 None
    # -out: error = 없음
    #------------------------------------------------------------------
    def plan_update(self, live_ids):
        if self.ensure_loaded() < 0:
            return None
        have = set(self.base["ids"][~self.deleted].tolist()) | {i for i, _ in self.add_tf}
        live = set(int(i) for i in live_ids)
        return sorted(live - have), sorted(have - live)

    #------------------------------------------------------------------
    # 합칠 때가 됐나 — 덧붙임이나 지움이 본의 5% 를 넘으면 True
    #
    # -in: n_add    = 이번에 더할 청크 수
    # -in: n_remove = 이번에 뺄 청크 수
    #
    # -out: True 면 전체 다시 만들기(build_tf)
    # -out: error = 없음 (색인이 없으면 True)
    #------------------------------------------------------------------
    def should_merge(self, n_add, n_remove):
        if not self.is_ready() or len(self.base["ids"]) == 0:
            return True
        n_base = len(self.base["ids"])
        return (len(self.add_tf) + n_add > MERGE_RATIO * n_base
                or int(self.deleted.sum()) + n_remove > MERGE_RATIO * n_base)

    #------------------------------------------------------------------
    # 고치기 — 새 청크는 덧붙이고, 지운 청크는 표시만 (단계 C)
    #    1) 뺄 id: 본에 있으면 지움 표시, 덧붙임에 있으면 덧붙임에서 뺀다
    #    2) 넣을 id: 덧붙임에 넣는다(같은 id 가 있으면 바꾼다)
    #    3) 덧붙임 · 통계를 다시 세고 덧붙임 파일만 쓴다(본 파일은 그대로)
    #
    # -in: add    = [(id, [(단어, 빈도)])]
    # -in: remove = 뺄 id 목록
    #
    # -out: 걸린 초
    # -out: error = 색인이 없으면 RuntimeError, 파일 저장 실패는 OSError 전파
    #------------------------------------------------------------------
    def update(self, add, remove):
        t0 = time.perf_counter()
        if self.ensure_loaded() < 0:
            raise RuntimeError("BM25 색인이 없어 고칠 수 없습니다")
        gone = set(int(i) for i in remove) | {int(i) for i, _ in add}
        if remove:
            self.deleted |= np.isin(self.base["ids"], np.asarray(list(remove), dtype=np.int64))
        # 덧붙임에서 빠질 것(지운 것 · 바꿀 것)을 뺀 뒤 새것을 넣는다
        self.add_tf = [x for x in self.add_tf if x[0] not in gone] + [(int(i), t) for i, t in add]
        self._rebuild_add()
        self._save_delta()
        return time.perf_counter() - t0

    #------------------------------------------------------------------
    # 규모 (status 표시용)
    #
    # -out: (본 청크 수, 덧붙임 청크 수, 지운 청크 수) — 색인이 없으면 None
    # -out: error = 없음
    #------------------------------------------------------------------
    def counts(self):
        if not self.is_ready():
            return None
        return len(self.base["ids"]), len(self.add_tf), int(self.deleted.sum())

    #------------------------------------------------------------------
    # 본 세그먼트 저장
    #=> npz 로 배열을 그대로 담는다. pickle 보다 적재가 빠르고 안전하다.
    #   vocab 은 토큰 배열 + term_id 배열로 나눠 저장한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 파일 쓰기 실패 시 OSError 전파
    #------------------------------------------------------------------
    def _save_base(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        b = self.base
        terms = np.array(list(b["vocab"].keys()), dtype=object)
        tids = np.asarray(list(b["vocab"].values()), dtype=np.int64)
        np.savez(self.path, version=np.int32(CACHE_VERSION), stamp=np.int64(self.stamp),
                 terms=terms, tids=tids, term_offset=b["term_offset"],
                 post_row=b["post_row"], post_tf=b["post_tf"], doc_len=b["doc_len"], ids=b["ids"])

    #------------------------------------------------------------------
    # 덧붙임 · 지움 저장 (본 파일은 그대로 — 문서 1건 반영이 ms 단위인 이유)
    #=> 덧붙임 tf 는 평탄한 배열(단어 · 빈도 · 청크별 구간)로 담는다. 본 표식을 함께 적는다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 파일 쓰기 실패 시 OSError 전파
    #------------------------------------------------------------------
    def _save_delta(self):
        words, freqs, offs = [], [], [0]
        for _, tf in self.add_tf:
            words.extend(t for t, _ in tf)
            freqs.extend(n for _, n in tf)
            offs.append(len(words))
        np.savez(self._delta_path(), version=np.int32(CACHE_VERSION), stamp=np.int64(self.stamp),
                 deleted=self.base["ids"][self.deleted],
                 add_ids=np.asarray([i for i, _ in self.add_tf], dtype=np.int64),
                 add_words=np.array(words, dtype=object), add_freqs=np.asarray(freqs, dtype=np.int64),
                 add_offs=np.asarray(offs, dtype=np.int64))

    #------------------------------------------------------------------
    # 덧붙임 · 지움 읽기
    #=> 표식이 지금 본과 다르면 다른 본의 찌꺼기다 — 읽지 않는다.
    #
    # -in: 없음
    #
    # -out: (지운 id 배열, 덧붙임 [(id, tf)]) — 없거나 짝이 안 맞거나 깨졌으면 None
    # -out: error = 없음
    #------------------------------------------------------------------
    def _load_delta(self):
        path = self._delta_path()
        if not os.path.isfile(path):
            return None
        try:
            z = np.load(path, allow_pickle=True)
            if int(z["version"]) != CACHE_VERSION or int(z["stamp"]) != self.stamp:
                return None
            words, freqs, offs = z["add_words"], z["add_freqs"], z["add_offs"]
            add = [(int(i), [(str(words[k]), int(freqs[k])) for k in range(offs[j], offs[j + 1])])
                   for j, i in enumerate(z["add_ids"])]
            return z["deleted"], add
        except Exception:
            return None                      # 깨진 덧붙임은 없는 것으로(다음 반영 때 다시 맞춘다)

    #------------------------------------------------------------------
    # 캐시에서 적재 보장(지연 로딩)
    #=> 캐시가 없거나 버전이 다르면 준비되지 않은 상태로 둔다
    #   (하이브리드가 dense 단독으로 자동 폴백한다). 같은 본의 덧붙임 파일이 있으면 함께 읽는다.
    #
    # -out: load_ms = 로딩 밀리초. 캐시 없음/불일치면 -1.0
    #------------------------------------------------------------------
    def ensure_loaded(self):
        if self.is_ready():
            return 0.0

        with self._load_lock:
            if self.is_ready():
                return 0.0

            path = self._base_file()
            if not os.path.isfile(path):
                return -1.0

            t0 = time.perf_counter()
            try:
                z = np.load(path, allow_pickle=True)
                if int(z["version"]) != CACHE_VERSION:
                    return -1.0
                base = {"vocab": {str(t): int(i) for t, i in zip(z["terms"], z["tids"])},
                        "term_offset": z["term_offset"], "post_row": z["post_row"],
                        "post_tf": z["post_tf"], "doc_len": z["doc_len"], "ids": z["ids"]}
                self.stamp = int(z["stamp"])
                self.deleted = np.zeros(len(base["ids"]), dtype=bool)
                self.base = base
                delta = self._load_delta()
                if delta is not None:
                    deleted, self.add_tf = delta
                    if len(deleted):
                        self.deleted = np.isin(base["ids"], deleted)
                self.ids = base["ids"]
                self._rebuild_add()
            except Exception:
                self.base = None           # 깨진 캐시는 없는 것으로 취급
                return -1.0
            return (time.perf_counter() - t0) * 1000.0

    #------------------------------------------------------------------
    # 검색 (핵심)
    #=> 질의 단어마다 두 세그먼트의 postings 만 훑는다. 전체 문서를 훑지 않는다.
    #    1) df = 지우지 않은 postings 수(본) + 덧붙임 postings 수 → idf
    #    2) postings 마다 그 자리에서 가중치를 계산해 더한다
    #
    # -in: query, tok, limit
    # -in: allow_ids = 이 점 id 들만 결과에 남긴다(폴더 한정 검색). None 이면 전체
    #
    # -out: [point_id, ...] 점수 순. 인덱스가 없으면 []
    #------------------------------------------------------------------
    def search(self, query, tok, limit, allow_ids=None):
        return [i for i, _ in self.search_scored(query, tok, limit, allow_ids)]

    #------------------------------------------------------------------
    # 검색 — (점 id, 점수) 점수 순 (search 의 본체 · 점수 동일성 시험용)
    #=> 같은 점수는 id 가 작은 쪽이 먼저(Rust 판과 같은 순서).
    #
    # -in: query, tok, limit, allow_ids = search 와 같음
    #
    # -out: [(point_id, score), ...] 점수 내림차순. 인덱스가 없으면 []
    # -out: error = 없음
    #------------------------------------------------------------------
    def search_scored(self, query, tok, limit, allow_ids=None):
        if self.ensure_loaded() < 0 or not self.is_ready() or self._n_live == 0:
            return []

        base, add = self.base, self.add
        n = np.float32(self._n_live)
        avgdl = np.float32(self._len_sum / self._n_live)
        k1, b = np.float32(config.BM25_K1), np.float32(config.BM25_B)     # config.yaml 에서 온다
        sc_base = np.zeros(len(base["ids"]), dtype=np.float32)
        sc_add = np.zeros(len(add["ids"]), dtype=np.float32)

        seen = set()
        for tkn in tokenize(query, tok):
            if tkn in seen:                 # 같은 토큰을 두 번 더하지 않는다
                continue
            seen.add(tkn)
            parts = []
            for seg, deleted in ((base, self.deleted), (add, None)):
                tid = seg["vocab"].get(tkn)
                if tid is None:
                    continue
                s, e = int(seg["term_offset"][tid]), int(seg["term_offset"][tid + 1])
                rows, tf = seg["post_row"][s:e], seg["post_tf"][s:e]
                if deleted is not None:
                    # 지운 청크는 빼고 df 를 센다 — 그래야 전체 재구축과 점수가 같다
                    live = ~deleted[rows]
                    rows, tf = rows[live], tf[live]
                parts.append((seg, rows, tf))
            df = np.float32(sum(len(r) for _, r, _ in parts))
            if df == 0:
                continue
            idf = np.log((n - df + np.float32(0.5)) / (df + np.float32(0.5)) + np.float32(1.0)).astype(np.float32)
            for seg, rows, tf in parts:
                dl = seg["doc_len"][rows]
                denom = tf + k1 * (np.float32(1.0) - b + b * dl / max(avgdl, np.float32(1e-9)))
                w = (idf * tf * (k1 + np.float32(1.0)) / denom).astype(np.float32)
                # (term, doc) 쌍은 유일하므로 fancy-index 누산이 안전하다.
                (sc_base if seg is base else sc_add)[rows] += w

        ids = np.concatenate([base["ids"], add["ids"]])
        scores = np.concatenate([sc_base, sc_add])
        # 폴더 한정 검색: 폴더 밖 청크는 점수를 지운다(6만 청크에서 2ms 안팎 — 설계서 §4)
        if allow_ids is not None:
            scores[~np.isin(ids, allow_ids)] = 0.0

        pos = np.flatnonzero(scores > 0)
        if not len(pos):
            return []

        # k 번째 점수 이상만 후보로 — 경계의 동점은 모두 남겨 id 순으로 고른다(실행마다 같은 결과)
        k = min(limit, len(pos))
        if len(pos) > k:
            kth = np.partition(scores[pos], len(pos) - k)[len(pos) - k]
            pos = pos[scores[pos] >= kth]
        # 점수 내림차순, 같은 점수는 id 오름차순
        top = pos[np.lexsort((ids[pos], -scores[pos]))][:k]
        return [(int(ids[i]), float(scores[i])) for i in top]
