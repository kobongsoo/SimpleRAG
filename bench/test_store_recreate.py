#------------------------------------------------------------------
# 인덱스 전체 재구축(recreate) 회귀 테스트 (REPORT §35)
#=> `index --rebuild` 가 옛 점을 지우지 못하고 새 청크와 섞던 버그를 막는다.
#   qdrant-client local 모드의 delete_collection 이 Windows 에서 열린 sqlite 때문에 폴더를
#   못 지우고(오류 무시) create_collection 이 옛 폴더를 다시 열어 점이 되살아났다.
#   작은 임시 컬렉션(4차원 벡터)으로 재현·확인하므로 모델·운영 인덱스를 건드리지 않는다.
#
#   실행: .venv/Scripts/python.exe bench/test_store_recreate.py
#------------------------------------------------------------------

import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag.index.store import COLLECTION, VectorStore   # noqa: E402

FAILED = []
TOTAL = [0]


#------------------------------------------------------------------
# 단언 도우미 — 실패해도 멈추지 않고 끝까지 돌린다
#
# -in: name = 테스트 이름
# -in: cond = 참이어야 하는 조건
# -in: note = 실패 시 함께 출력할 설명
#
# -out: 없음
# -out: error = 없음 (실패는 FAILED 에 쌓는다)
#------------------------------------------------------------------
def check(name, cond, note=""):
    TOTAL[0] += 1
    print(("  OK   %s" if cond else "  FAIL %s  " + str(note)) % name)
    if not cond:
        FAILED.append(name)


#------------------------------------------------------------------
# 점 n 개 넣기
#
# -in: store = VectorStore
# -in: start = 시작 id
# -in: n     = 개수
# -in: tag   = payload 의 doc_path (옛/새 구분용)
#
# -out: 없음
# -out: error = 저장 실패 시 qdrant 예외 전파
#------------------------------------------------------------------
def put(store, start, n, tag):
    ids = list(range(start, start + n))
    vecs = np.random.RandomState(start).rand(n, 4).astype(np.float32)
    store.upsert(ids, vecs, [{"text": "t%d" % i, "doc_path": tag, "doc_name": tag,
                              "chunk_idx": i, "mtime": 0} for i in ids])


def count(store):
    return store._client.count(COLLECTION).count


def main():
    tmp = tempfile.mkdtemp(prefix="simplerag_store_")
    path = os.path.join(tmp, "qdrant_data")
    try:
        print("\n[옛 인덱스 만들기]")
        st = VectorStore(path=path, dim=4)
        st.ensure_collection()
        put(st, 0, 50, "옛문서")
        st.close()
        st = VectorStore(path=path, dim=4)
        st.ensure_loaded()
        check("옛 점 50개가 디스크에 저장됨", count(st) == 50, count(st))

        print("\n[recreate — 같은 프로세스에서 이미 연 상태로]")
        st.ensure_collection(recreate=True)
        check("recreate 직후 점 0개", count(st) == 0, count(st))
        put(st, 0, 3, "새문서")                        # 운영처럼 id 0 부터 다시
        check("새 점 3개만 있음", count(st) == 3, count(st))
        st.close()

        print("\n[다시 열어도 옛 점이 되살아나지 않는가 — 실제로 터진 경로]")
        st = VectorStore(path=path, dim=4)
        st.ensure_loaded()
        n = count(st)
        pts, _ = st._client.scroll(collection_name=COLLECTION, limit=100, with_payload=True)
        tags = {p.payload["doc_path"] for p in pts}
        check("다시 연 뒤에도 3개", n == 3, n)
        check("옛 문서 점이 섞이지 않음", tags == {"새문서"}, tags)
        st.close()

        print("\n[빈 폴더에서 recreate — 처음 인덱싱]")
        st = VectorStore(path=os.path.join(tmp, "fresh"), dim=4)
        st.ensure_collection(recreate=True)
        check("새 폴더 recreate 는 오류 없이 0개", count(st) == 0)
        st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d개 중 %d개 실패" % (TOTAL[0], len(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
