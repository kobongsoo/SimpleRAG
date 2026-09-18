#------------------------------------------------------------------
# 벡터 한 벌 + 폴더 표시 (폴더 한정 검색 설계서 §5 — FS1)
#=> 폴더 패널에서 한 질문은 그 폴더(하위 포함) 문서의 청크만 근거로 쓴다.
#
#   왜 Qdrant 필터를 쓰지 않나
#     local 모드는 필터를 걸면 점마다 조건을 파이썬으로 검사한다. 6만 청크에서
#     검색이 114ms → 730~920ms 로 느려졌다(설계서 §4 실측).
#
#   그래서
#    - 인덱스의 모든 청크 벡터를 float32 행렬 "한 벌" 로 둔다(청크당 1.5KB, 본문은 안 든다)
#    - 폴더마다 "이 청크가 그 폴더 것인가" 참/거짓 배열(청크당 1바이트)만 만든다
#    - 질문: 행렬 · 질의벡터 → 표시 밖은 빼고 → 상위 k (6만 청크 2.9ms 실측)
#   폴더를 몇 번 오가도 행렬은 한 벌 그대로다. 표시는 최근 8개만 둔다.
#   워커가 내려가면(프로세스 종료) 모두 풀린다.
#
#   ⚠️ 스레드: 만들기(build)는 예열 스레드에서, 고치기(update/remove)와 찾기는 워커의
#      대화 루프(한 스레드)에서 한다. 둘이 겹치지 않게 잠금으로 감싼다.
#------------------------------------------------------------------

import collections
import os
import threading
import time

import numpy as np

# 행렬이 이보다 크면 만들지 않는다 — 그때는 느리지만 메모리를 안 쓰는 Qdrant 필터로 찾는다.
# 15만 청크(약 230MB)는 SimpleRAG 가 서버 모드로 옮기기로 한 규모다(설계서 결정5).
DEFAULT_MAX_MB = float(os.environ.get("SIMPLERAG_MATRIX_MAX_MB", "300"))
MASK_CACHE = 8                # 최근 폴더 표시를 이만큼만 둔다
REBUILD_DEAD_RATIO = 0.2      # 지워진 행이 이 비율을 넘으면 새로 만든다


#------------------------------------------------------------------
# 폴더 비교용 표기
#=> 대소문자·끝 구분자를 맞춘다. 자동 인덱싱의 경로 비교와 같은 규칙이다.
#
# -in: path = 경로
#
# -out: 비교용 문자열
# -out: error = 없음
#------------------------------------------------------------------
def norm_dir(path):
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


#------------------------------------------------------------------
# 문서가 그 폴더 아래(하위 포함)에 있는가
#=> "D:\규정" 이 "D:\규정2\a.doc" 을 품지 않게, 폴더 뒤에 구분자를 붙여 비교한다.
#
# -in: doc_norm   = normcase 한 문서 경로
# -in: folder_norm= norm_dir 한 폴더
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def under(doc_norm, folder_norm):
    return doc_norm.startswith(folder_norm + os.sep)


#------------------------------------------------------------------
# 벡터 행렬
#
# -필드: ids      = 행 → 점 id (int64)
# -필드: mat      = 행 → 정규화된 벡터 (float32, n×dim)
# -필드: alive    = 행이 아직 쓰이는가(문서를 지우거나 고치면 옛 행은 False)
# -필드: disabled = 만들지 않은 이유(너무 큼 등). 있으면 Qdrant 필터로 찾는다
# -필드: version  = 고칠 때마다 1 늘어난다(폴더 표시 캐시를 버리는 기준)
#------------------------------------------------------------------
class VectorMatrix:
    #--------------------------------------------------------------
    # 생성자 — 아직 아무것도 읽지 않는다
    #
    # -in: max_mb = 행렬 크기 한도(MB)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, max_mb=None):
        self.max_mb = DEFAULT_MAX_MB if max_mb is None else float(max_mb)
        self.ids = None
        self.mat = None
        self.alive = None
        self.disabled = None
        self.version = 0
        self.build_ms = None
        self._doc_rows = {}          # {doc_path: [행 번호…]}
        self._doc_norm = {}          # {doc_path: normcase 경로} — 매번 계산하지 않게
        self._masks = collections.OrderedDict()   # {(폴더, version): (마스크, 문서 수)}
        self._lock = threading.RLock()

    def ready(self):
        return self.mat is not None

    #--------------------------------------------------------------
    # 없으면 만들기 (예열 스레드에서도, 첫 폴더 질문에서도 부른다)
    #
    # -in: store = VectorStore (적재된 것)
    #
    # -out: 걸린 ms (이미 있으면 0, 한도 초과면 -1)
    # -out: error = 저장소 읽기 실패는 예외 전파
    #--------------------------------------------------------------
    def ensure_built(self, store):
        with self._lock:
            if self.ready():
                return 0.0
            if self.disabled:
                return -1.0
            return self._build(store)

    #--------------------------------------------------------------
    # 행렬 만들기
    #=> 1) 청크 수 × 차원으로 크기를 먼저 어림해 한도를 넘으면 만들지 않는다
    #   2) 모든 점을 벡터와 doc_path 만 붙여 훑는다(본문은 읽지 않는다)
    #   3) 코사인 비교가 내적 한 번이 되도록 정규화한다
    #
    # -in: store = VectorStore
    #
    # -out: 걸린 ms (한도 초과면 -1)
    # -out: error = 저장소 읽기 실패는 예외 전파
    #--------------------------------------------------------------
    def _build(self, store):
        from ..index.store import COLLECTION

        t0 = time.perf_counter()
        store.ensure_collection()
        n = store._client.count(COLLECTION).count
        est_mb = n * store.dim * 4 / 1e6
        if est_mb > self.max_mb:
            self.disabled = "청크 {:,}개 → 행렬 {:.0f}MB 가 한도 {:.0f}MB 를 넘음".format(n, est_mb, self.max_mb)
            return -1.0

        ids, vecs, doc_rows = [], [], {}
        offset = None
        while True:
            pts, offset = store._client.scroll(
                collection_name=COLLECTION, limit=2000, offset=offset,
                with_payload=["doc_path"], with_vectors=True)
            for p in pts:
                doc_rows.setdefault(p.payload.get("doc_path", ""), []).append(len(ids))
                ids.append(p.id)
                vecs.append(p.vector)
            if offset is None:
                break

        mat = np.asarray(vecs, dtype=np.float32).reshape(len(ids), -1) if ids else \
            np.zeros((0, store.dim), dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        mat /= np.maximum(norms, 1e-12)
        self.ids = np.asarray(ids, dtype=np.int64)
        self.mat = mat
        self.alive = np.ones(len(ids), dtype=bool)
        self._doc_rows = doc_rows
        self._doc_norm = {d: os.path.normcase(d) for d in doc_rows}
        self._masks.clear()
        self.version += 1
        self.build_ms = round((time.perf_counter() - t0) * 1000, 1)
        return self.build_ms

    #--------------------------------------------------------------
    # 문서 한 건이 바뀌었을 때 (자동 인덱싱 /index-doc 뒤)
    #=> 옛 행은 "없음" 으로 표시하고 새 청크의 벡터를 덧붙인다. 지워진 행이 많아지면
    #   새로 만든다(메모리가 옛 행만큼 늘어난 채로 있지 않게).
    #
    # -in: store    = VectorStore
    # -in: doc_path = 문서 경로(payload 의 doc_path 와 같은 표기)
    # -in: new_ids  = 새로 넣은 점 id 목록(없으면 지우기만)
    #
    # -out: 없음
    # -out: error = 저장소 읽기 실패는 예외 전파(부르는 쪽이 잡는다)
    #--------------------------------------------------------------
    def update_doc(self, store, doc_path, new_ids):
        with self._lock:
            if not self.ready():
                return
            for r in self._doc_rows.pop(doc_path, []):
                self.alive[r] = False
            self._doc_norm.pop(doc_path, None)
            if new_ids:
                from ..index.store import COLLECTION
                pts = store._client.retrieve(collection_name=COLLECTION, ids=[int(i) for i in new_ids],
                                             with_payload=False, with_vectors=True)
                if pts:
                    add = np.asarray([p.vector for p in pts], dtype=np.float32)
                    add /= np.maximum(np.linalg.norm(add, axis=1, keepdims=True), 1e-12)
                    start = len(self.ids)
                    self.mat = np.vstack([self.mat, add])
                    self.ids = np.concatenate([self.ids, np.asarray([p.id for p in pts], dtype=np.int64)])
                    self.alive = np.concatenate([self.alive, np.ones(len(pts), dtype=bool)])
                    self._doc_rows[doc_path] = list(range(start, start + len(pts)))
                    self._doc_norm[doc_path] = os.path.normcase(doc_path)
            self.version += 1
            self._masks.clear()
            dead = int((~self.alive).sum())
            if dead and dead > REBUILD_DEAD_RATIO * len(self.alive):
                self.mat = None
                self._build(store)

    #--------------------------------------------------------------
    # 문서 한 건이 지워졌을 때
    #
    # -in: doc_path = 문서 경로
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def remove_doc(self, doc_path):
        with self._lock:
            if not self.ready():
                return
            for r in self._doc_rows.pop(doc_path, []):
                self.alive[r] = False
            self._doc_norm.pop(doc_path, None)
            self.version += 1
            self._masks.clear()

    #--------------------------------------------------------------
    # 폴더 표시 만들기 (캐시)
    #=> 그 폴더 아래 문서의 행에 True. 최근 MASK_CACHE 개만 둔다.
    #
    # -in: folder = 폴더
    #
    # -out: (마스크 bool 배열, 문서 수)
    # -out: error = 없음 (행렬이 없으면 (None, 0))
    #--------------------------------------------------------------
    def mask(self, folder):
        with self._lock:
            if not self.ready():
                return None, 0
            f = norm_dir(folder)
            key = (f, self.version)
            hit = self._masks.get(key)
            if hit is not None:
                self._masks.move_to_end(key)
                return hit
            m = np.zeros(len(self.ids), dtype=bool)
            n_docs = 0
            for doc, rows in self._doc_rows.items():
                if under(self._doc_norm.get(doc) or os.path.normcase(doc), f):
                    m[rows] = True
                    n_docs += 1
            m &= self.alive
            self._masks[key] = (m, n_docs)
            while len(self._masks) > MASK_CACHE:
                self._masks.popitem(last=False)
            return m, n_docs

    #--------------------------------------------------------------
    # 표시 안에서 가장 가까운 청크 찾기
    #
    # -in: qvec = 질의 벡터 (dim,)
    # -in: k    = 개수
    # -in: m    = 폴더 마스크(None 이면 살아 있는 행 전부)
    #
    # -out: [(점 id, 점수)…] 점수 내림차순
    # -out: error = 없음 (행렬이 없으면 빈 목록)
    #--------------------------------------------------------------
    def search(self, qvec, k, m=None):
        with self._lock:
            if not self.ready() or not len(self.ids):
                return []
            q = np.asarray(qvec, dtype=np.float32)
            q = q / max(float(np.linalg.norm(q)), 1e-12)
            scores = self.mat @ q
            keep = self.alive if m is None else m
            scores = np.where(keep, scores, -np.inf)
            n_keep = int(keep.sum())
            if n_keep == 0:
                return []
            k = min(k, n_keep)
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            return [(int(self.ids[i]), float(scores[i])) for i in top]

    #--------------------------------------------------------------
    # 표시 안 점 id 들 (BM25 에 넘긴다)
    #
    # -in: m = 폴더 마스크
    #
    # -out: int64 배열
    # -out: error = 없음
    #--------------------------------------------------------------
    def allowed_ids(self, m):
        with self._lock:
            return self.ids[m] if m is not None else self.ids[self.alive]

    #--------------------------------------------------------------
    # 메모리(MB) — 로그·시험용
    #
    # -in: 없음
    #
    # -out: MB
    # -out: error = 없음
    #--------------------------------------------------------------
    def nbytes_mb(self):
        if not self.ready():
            return 0.0
        return (self.mat.nbytes + self.ids.nbytes + self.alive.nbytes) / 1e6
