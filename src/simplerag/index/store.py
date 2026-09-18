#------------------------------------------------------------------
# Qdrant local 모드 벡터 저장소 (설계서 결정5)
#=> 별도 서버 프로세스 없이 파일 기반으로 동작한다. 공식 문서는 2만 포인트
#   초과 시 서버 모드를 권하지만, 5만 청크 실측에서 검색 78~106ms 로
#   LLM TTFT(2,330ms) 대비 4% 에 불과해 병목이 아니다. 서버를 띄우는 대가
#   (프로세스 생명주기/포트/방화벽)가 이득보다 크다.
#
#   전환 기준: 청크 15만 초과 또는 검색 300ms 초과 → 서버 모드(API 동일).
#------------------------------------------------------------------

import os
import threading
import time

from .. import config

COLLECTION = "rag"


class VectorStore:
    #------------------------------------------------------------------
    # 생성자 — 경로만 보관(클라이언트는 아직 생성 안 함)
    #
    # -in: path = 인덱스 폴더(None 이면 설정값)
    # -in: dim  = 벡터 차원(None 이면 설정값)
    #------------------------------------------------------------------
    def __init__(self, path=None, dim=None):
        self.path = path or config.QDRANT_DIR
        self.dim = dim or config.EMBED.dim
        self._client = None
        self._load_lock = threading.Lock()

    def is_ready(self):
        return self._client is not None

    #------------------------------------------------------------------
    # 클라이언트 적재 보장(지연 로딩)
    #=> 5만 청크 인덱스 기준 로딩에 약 2.3초가 걸린다(콜드스타트 최대 항목).
    #   예열 스레드가 미리 불러 두는 것을 전제로 한다.
    #
    # -out: load_ms
    #------------------------------------------------------------------
    def ensure_loaded(self):
        if self.is_ready():
            return 0.0

        with self._load_lock:
            if self.is_ready():
                return 0.0
            import warnings

            from qdrant_client import QdrantClient

            t0 = time.perf_counter()
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

            # "2만 포인트 초과 시 local 모드 비권장" 경고를 억제한다.
            # 무시하는 것이 아니라 '측정하고 판단한' 결과다 — 43,058청크에서
            # dense 검색 81ms 로 전체 응답(2.5초)의 3% 에 불과했다(설계서 결정5).
            # 매 명령마다 두 줄씩 찍혀 실제 출력을 가리므로 여기서만 끈다.
            # 전환 기준(dense 300ms 초과 / 청크 15만 초과)은 `search` 명령의
            # 단계별 지연 출력으로 계속 감시할 수 있다.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message=".*Local mode is not recommended.*")
                self._client = QdrantClient(path=self.path)
            return (time.perf_counter() - t0) * 1000.0

    #------------------------------------------------------------------
    # 컬렉션 보장
    #=> 없으면 만든다. recreate=True 면 지우고 새로 만든다(전체 재인덱싱).
    #
    #   🔴 recreate 가 조용히 실패하던 버그(REPORT §35) — qdrant-client 1.19 local 모드의
    #      delete_collection 은 컬렉션 폴더를 rmtree(ignore_errors=True) 로 지우는데, 그때
    #      컬렉션의 sqlite 파일이 아직 열려 있다. Windows 는 열린 파일을 못 지우고 오류는
    #      무시되므로 폴더가 그대로 남고, create_collection 이 그 폴더를 다시 열어 **옛 점이
    #      전부 되살아났다.** 실제로 `index --rebuild` 가 5만 9천 점을 그대로 둔 채 새 청크를
    #      id 0번부터 덮어써 옛 문서와 새 문서가 섞였다.
    #      → 폴더가 남았으면 클라이언트를 닫아 핸들을 놓게 한 뒤 직접 지우고(실패는 예외),
    #        다시 연다. 끝으로 점이 0개인지 확인한다 — 다시는 조용히 섞이지 않게.
    #
    # -in: recreate = 기존 컬렉션 삭제 후 재생성 여부
    #------------------------------------------------------------------
    def ensure_collection(self, recreate=False):
        from qdrant_client.models import Distance, VectorParams

        self.ensure_loaded()
        exists = self._client.collection_exists(COLLECTION)

        if exists and recreate:
            self._client.delete_collection(COLLECTION)
            exists = False
            # 라이브러리가 못 지운 폴더를 확실히 지운다(위 🔴 설명)
            self._purge_collection_dir()
        if not exists:
            self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE))
        if recreate:
            left = self._client.count(COLLECTION).count
            if left:
                raise RuntimeError("컬렉션을 비우지 못했습니다(점 {:,}개 남음): {}".format(
                    left, self.path))

    #------------------------------------------------------------------
    # 남은 컬렉션 폴더 직접 지우기 (recreate 보조)
    #=> qdrant local 이 sqlite 를 연 채 지우려다 실패한 폴더를 처리한다.
    #    1) 폴더가 없으면 할 일 없음(리눅스 등 정상 삭제된 경우)
    #    2) 클라이언트를 닫고 gc 로 핸들을 놓게 한다 — 닫지 않으면 Windows 가 거부한다
    #    3) 폴더를 지운다. 실패하면 예외를 그대로 올린다(조용히 넘어가지 않는다)
    #    4) 클라이언트를 다시 연다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 폴더 삭제 실패 시 OSError 전파
    #------------------------------------------------------------------
    def _purge_collection_dir(self):
        import gc
        import shutil

        coll_dir = os.path.join(self.path, "collection", COLLECTION)
        if not os.path.isdir(coll_dir):
            return
        self.close()
        gc.collect()
        shutil.rmtree(coll_dir)
        self.ensure_loaded()

    #------------------------------------------------------------------
    # 청크 적재 (핵심)
    #=> payload 에 본문과 출처를 함께 넣는다. 2단계 UX 가 근거를 즉시 화면에
    #   띄우려면 검색 결과만으로 본문이 나와야 하기 때문이다(별도 조회 금지).
    #
    # -in: ids     = 포인트 id 목록(전역 고유)
    # -in: vectors = (N, dim) 배열
    # -in: payloads= dict 목록 {text, doc_path, doc_name, chunk_idx, mtime}
    # -in: batch   = 한 번에 보낼 개수
    #------------------------------------------------------------------
    def upsert(self, ids, vectors, payloads, batch=1000):
        from qdrant_client.models import PointStruct

        self.ensure_collection()
        for i in range(0, len(ids), batch):
            pts = [
                PointStruct(id=int(ids[j]),
                            vector=vectors[j].tolist(),
                            payload=payloads[j])
                for j in range(i, min(i + batch, len(ids)))
            ]
            self._client.upsert(collection_name=COLLECTION, points=pts)

    #------------------------------------------------------------------
    # 문서 단위 삭제 (증분 갱신용)
    #=> 파일이 바뀌면 그 문서의 청크만 지우고 다시 넣는다.
    #
    # -in: doc_path = 삭제할 문서 경로
    #------------------------------------------------------------------
    def delete_doc(self, doc_path):
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        self.ensure_collection()
        self._client.delete(
            collection_name=COLLECTION,
            points_selector=Filter(must=[
                FieldCondition(key="doc_path", match=MatchValue(value=doc_path))
            ]))

    #------------------------------------------------------------------
    # 문서의 청크 가운데 "방금 넣은 것" 만 남기고 지우기 (자동 인덱싱 §5)
    #=> 수정된 문서를 바꿀 때 새 청크를 먼저 넣고 부른다. 옛 버전은 물론,
    #   예전에 넣다가 중간에 끊겨 남은 찌꺼기까지 한 번에 지운다.
    #   (먼저 지우고 넣으면 그 사이 문서가 검색에서 사라지고, 넣기가 실패하면
    #    문서가 통째로 없어진다 — 그래서 순서를 뒤집었다.)
    #    1) 조건: doc_path 가 이 문서이고, id 가 keep_ids 에 없음
    #
    # -in: doc_path = 문서 경로(payload 의 doc_path 와 같은 표기)
    # -in: keep_ids = 남길 점 id 목록(방금 넣은 것)
    #
    # -out: 없음
    # -out: error = qdrant 예외 전파
    #------------------------------------------------------------------
    def delete_doc_except(self, doc_path, keep_ids):
        from qdrant_client.models import FieldCondition, Filter, HasIdCondition, MatchValue

        self.ensure_collection()
        must_not = [HasIdCondition(has_id=[int(i) for i in keep_ids])] if keep_ids else []
        self._client.delete(
            collection_name=COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="doc_path", match=MatchValue(value=doc_path))],
                must_not=must_not))

    #------------------------------------------------------------------
    # 점 id 로 지우기 (고아 청소용)
    #
    # -in: ids = 지울 점 id 목록
    #
    # -out: 없음
    # -out: error = qdrant 예외 전파
    #------------------------------------------------------------------
    def delete_ids(self, ids):
        from qdrant_client.models import PointIdsList

        if not ids:
            return
        self.ensure_collection()
        for i in range(0, len(ids), 1000):
            self._client.delete(
                collection_name=COLLECTION,
                points_selector=PointIdsList(points=[int(x) for x in ids[i:i + 1000]]))

    #------------------------------------------------------------------
    # 벡터 검색
    #=> payload 를 함께 받아 본문까지 한 번에 돌려준다.
    #
    # -in: vector    = 질의 벡터 (dim,)
    # -in: limit     = 반환 개수
    # -in: doc_paths = 이 문서들의 청크에서만 찾는다(폴더 한정 검색의 느린 길).
    #                  ⚠️ local 모드 필터는 점마다 파이썬으로 검사해 6만 청크에서 8배 느리다 —
    #                  벡터 한 벌(VectorMatrix)을 못 만들었을 때만 쓴다
    #
    # -out: [(point_id, score, payload), ...] 점수 내림차순
    #------------------------------------------------------------------
    def search(self, vector, limit, doc_paths=None):
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        self.ensure_collection()
        flt = None
        if doc_paths is not None:
            if not doc_paths:
                return []
            flt = Filter(must=[FieldCondition(key="doc_path", match=MatchAny(any=list(doc_paths)))])
        res = self._client.query_points(
            collection_name=COLLECTION, query=vector.tolist(),
            limit=limit, with_payload=True, query_filter=flt)
        return [(p.id, p.score, p.payload) for p in res.points]

    #------------------------------------------------------------------
    # 문서들의 점 id 모으기 (폴더 한정 검색의 느린 길)
    #
    # -in: doc_paths = 문서 경로 목록
    #
    # -out: 점 id 목록
    # -out: error = qdrant 예외 전파
    #------------------------------------------------------------------
    def ids_of_docs(self, doc_paths):
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        if not doc_paths:
            return []
        self.ensure_collection()
        flt = Filter(must=[FieldCondition(key="doc_path", match=MatchAny(any=list(doc_paths)))])
        out, offset = [], None
        while True:
            pts, offset = self._client.scroll(collection_name=COLLECTION, scroll_filter=flt,
                                              limit=5000, offset=offset,
                                              with_payload=False, with_vectors=False)
            out.extend(p.id for p in pts)
            if offset is None:
                return out

    #------------------------------------------------------------------
    # 인덱스 통계
    # -out: dict = {count, disk_mb}
    #------------------------------------------------------------------
    def stats(self):
        self.ensure_loaded()
        if not self._client.collection_exists(COLLECTION):
            return {"count": 0, "disk_mb": 0.0}

        count = self._client.count(COLLECTION).count
        size = 0
        for root, _, files in os.walk(self.path):
            for f in files:
                size += os.path.getsize(os.path.join(root, f))
        return {"count": count, "disk_mb": round(size / 1024 / 1024, 1)}

    #------------------------------------------------------------------
    # 닫기
    #=> local 모드는 폴더에 락을 잡으므로 프로세스 종료 전에 놓아 준다.
    #------------------------------------------------------------------
    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
