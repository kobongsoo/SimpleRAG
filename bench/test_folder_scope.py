#------------------------------------------------------------------
# 폴더 한정 검색 회귀 테스트 (plan/폴더한정검색_설계서.html — FS1)
#=> 폴더를 주면 그 폴더(하위 포함) 문서의 청크만 근거로 나오는지 본다.
#   모델을 올리지 않는다: 임베딩은 가짜(test_auto_index 의 FakeEmbedder), 토크나이저는 진짜.
#   Qdrant·BM25·상태 파일은 임시 폴더에 만든다 — 운영 인덱스를 건드리지 않는다.
#
#   실행: .venv/Scripts/python.exe bench/test_folder_scope.py
#------------------------------------------------------------------

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "src")
sys.path.insert(0, "bench")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                                    # noqa: E402
from simplerag.index import indexer                             # noqa: E402
from simplerag.index.bm25 import Bm25Index                      # noqa: E402
from simplerag.index.store import VectorStore                   # noqa: E402
from simplerag.pipeline import NO_DOCS_IN_FOLDER, RagPipeline   # noqa: E402
from simplerag.retrieve.hybrid import HybridRetriever           # noqa: E402
from simplerag.retrieve.vector_matrix import VectorMatrix       # noqa: E402
from test_auto_index import FakeEmbedder, bump, check, write_doc, FAILED, TOTAL  # noqa: E402


#------------------------------------------------------------------
# 근거의 문서 경로들
#
# -in: chunks = 검색 결과
#
# -out: doc_path 집합
# -out: error = 없음
#------------------------------------------------------------------
def paths(chunks):
    return {c["doc_path"] for c in chunks}


def main():
    tmp = tempfile.mkdtemp(prefix="simplerag_scope_")
    root = os.path.join(tmp, "docs")
    config.STATE_PATH = os.path.join(tmp, "index_state.json")
    emb = FakeEmbedder()
    store = VectorStore(path=os.path.join(tmp, "qdrant"), dim=4)
    bm25 = Bm25Index(path=os.path.join(tmp, "bm25.npz"))
    A, A_SUB, B, A2 = (os.path.join(root, d) for d in ("A", os.path.join("A", "인사"), "B", "A2"))
    EMPTY = os.path.join(root, "빈폴더")
    os.makedirs(EMPTY)
    # 네 폴더 모두 "출장 여비" 글을 넣는다 — 전체 검색이면 섞여 나와야 시험이 뜻이 있다
    docs = {os.path.join(A, "a1.txt"): "사과", os.path.join(A_SUB, "a2.txt"): "배",
            os.path.join(B, "b1.txt"): "감", os.path.join(A2, "c1.txt"): "귤"}
    try:
        for p, m in docs.items():
            write_doc(p, m, paras=2)
        indexer.index_folder(root, emb, store, bm25)

        mat = VectorMatrix()
        r = HybridRetriever(emb, store, bm25, matrix=mat)
        q = "출장 여비는 규정에 따라 지급한다"

        print("\n[1. 폴더 한정 — 벡터 한 벌]")
        allc, _ = r.search(q, top_k=20)
        check("전체 검색은 여러 폴더가 섞인다(시험 전제)", len({os.path.dirname(p) for p in paths(allc)}) >= 3,
              paths(allc))
        ca, ta = r.search(q, top_k=20, folder=A)
        check("A 폴더: 근거가 모두 A 아래(하위 포함)",
              ca and all(p.startswith(A + os.sep) for p in paths(ca)), paths(ca))
        check("A 폴더: 하위 폴더(A\\인사) 문서도 포함", os.path.join(A_SUB, "a2.txt") in paths(ca))
        check("A 폴더: 이름이 겹치는 A2 는 안 섞인다", not any(p.startswith(A2) for p in paths(ca)))
        check("A 폴더: 벡터 한 벌로 찾았다", ta.get("scope_via") == "matrix", ta)
        check("A 폴더: 문서 수 2", ta.get("scope_docs") == 2, ta)
        cs, ts = r.search(q, top_k=20, folder=A_SUB)
        check("A\\인사: 그 하위 문서만", paths(cs) == {os.path.join(A_SUB, "a2.txt")}, paths(cs))
        cb, _ = r.search(q, top_k=20, folder=B + os.sep)
        check("B\\ (끝 구분자) 도 같게 본다", paths(cb) == {os.path.join(B, "b1.txt")}, paths(cb))
        cu, _ = r.search(q, top_k=20, folder=A.upper())
        check("대소문자가 달라도 같은 폴더", paths(cu) == paths(ca), paths(cu))

        print("\n[2. 빈 폴더 — 모델을 부르지 않고 알린다]")
        ce, te = r.search(q, top_k=5, folder=EMPTY)
        check("빈 폴더: 근거 0 · scope_docs 0", ce == [] and te.get("scope_docs") == 0, te)

        class NoGen:
            def stream(self, *a, **k):
                raise AssertionError("모델을 부르면 안 된다")
        pipe = RagPipeline(r, NoGen())
        evs = list(pipe.answer(q, folder=EMPTY))
        check("빈 폴더: 안내 문구로 끝난다", evs[-1][0] == "done" and evs[-1][1]["answer"] == NO_DOCS_IN_FOLDER)

        print("\n[3. 느린 길(Qdrant 필터)도 결과가 같다]")
        slow = HybridRetriever(emb, store, bm25, matrix=VectorMatrix(max_mb=0))
        cf, tf = slow.search(q, top_k=20, folder=A)
        check("한도 0 → 벡터 한 벌 없이 필터로", tf.get("scope_via") == "filter", tf)
        check("필터 길도 같은 문서들", paths(cf) == paths(ca), (paths(cf), paths(ca)))

        print("\n[4. 워커 명령이 벡터 한 벌을 고친다]")
        import types
        from simplerag.index import commands
        app = types.SimpleNamespace(embedder=emb, store=store, bm25=bm25, matrix=mat)
        ic = commands.IndexCommands(app)
        v0 = mat.version
        a1 = os.path.join(A, "a1.txt")
        write_doc(a1, "사과수정", paras=1)
        bump(a1, 30)
        res = ic.run("/index-doc", json.dumps({"path": a1}))
        check("index-doc 수정", res["ok"] and res["result"] == "modified", res)
        check("벡터 한 벌이 고쳐졌다(version 증가)", mat.version > v0)
        ic.run("/bm25", "")          # 실제 흐름처럼 반영 뒤 키워드 색인도 다시 만든다
        ca2, _ = r.search("사과수정 출장", top_k=20, folder=A)
        texts = [c["text"] for c in ca2 if c["doc_path"] == a1]
        check("A 폴더 검색에 새 내용만", texts and all("사과수정" in t for t in texts), texts[:1])
        new = os.path.join(A, "새문서.txt")
        write_doc(new, "포도")
        res = ic.run("/index-doc", new)
        ca3, t3 = r.search(q, top_k=20, folder=A)
        check("새 문서가 A 폴더 범위에 들어온다", new in paths(ca3) and t3["scope_docs"] == 3, t3)
        os.remove(new)
        ic.run("/remove-doc", new)
        ca4, t4 = r.search(q, top_k=20, folder=A)
        check("지운 문서는 A 폴더 범위에서 빠진다", new not in paths(ca4) and t4["scope_docs"] == 2, t4)

        print("\n[5. 폴더를 오가도 메모리가 늘지 않는다]")
        mb0 = mat.nbytes_mb()
        for _ in range(30):
            for f in (A, A_SUB, B, A2, EMPTY, root):
                r.search(q, top_k=3, folder=f)
        check("벡터 한 벌 크기 그대로", mat.nbytes_mb() == mb0, (mb0, mat.nbytes_mb()))
        check("폴더 표시는 최근 8개만", len(mat._masks) <= 8, len(mat._masks))

        print("\n[6. BM25 허용 id]")
        import numpy as np
        m, _ = mat.mask(B)
        ids = bm25.search("감 출장 여비", emb.tokenizer, 50, allow_ids=mat.allowed_ids(m))
        allowed = set(int(x) for x in mat.allowed_ids(m))
        check("BM25 결과가 모두 허용 id 안", ids and set(ids) <= allowed, (ids[:5], len(allowed)))

        print("\n[7. 지운 행이 많으면 새로 만든다]")
        for rnd in range(3):
            for p in list(docs)[:3]:
                write_doc(p, "대량수정{}".format(rnd), paras=2)
                bump(p, 100 + rnd * 10)
                ic.run("/index-doc", p)
        dead = int((~mat.alive).sum())
        check("여러 번 고쳐도 지워진 행은 20% 이하로 유지(넘으면 새로 만든다)",
              dead <= 0.2 * len(mat.alive), (dead, len(mat.alive)))
        live = int(mat.alive.sum())
        check("살아 있는 행 수 = 인덱스 청크 수", live == store.stats()["count"], (live, store.stats()["count"]))

        print("\n[8. 지정 폴더 밖 문서 정리 (인덱싱 폴더 = 패널 폴더)]")
        c1 = os.path.join(A2, "c1.txt")
        res = ic.run("/index-outside", json.dumps({"roots": [A, B]}))
        check("A·B 만 지정하면 A2 문서가 '밖'", res["ok"] and res["outside"] == [c1], res)
        res = ic.run("/index-outside", json.dumps({"roots": []}))
        check("지정 폴더가 비면 아무것도 '밖' 이라 하지 않는다", res["ok"] is False, res)
        res = ic.run("/remove-doc", json.dumps({"path": c1}))
        check("파일이 있으면 그냥은 안 뺀다", res["ok"] is False, res)
        res = ic.run("/remove-doc", json.dumps({"path": os.path.join(A, "a1.txt"), "outside_of": [A, B]}))
        check("지정 폴더 안 문서는 outside_of 로도 안 뺀다", res["ok"] is False, res)
        res = ic.run("/remove-doc", json.dumps({"path": c1, "outside_of": [A, B]}))
        check("지정 폴더 밖이면 파일이 있어도 뺀다", res["ok"] and res["result"] == "removed", res)
        ca5, _ = r.search(q, top_k=20)
        check("뺀 뒤 전체 검색에도 안 나온다", c1 not in paths(ca5))
        res = ic.run("/index-outside", json.dumps({"roots": [root]}))
        check("상위 폴더 하나로 지정하면 '밖' 없음", res["ok"] and res["outside"] == [], res)
    finally:
        store.close()
        time.sleep(0.2)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== {}/{} 통과 ===".format(TOTAL[0] - len(FAILED), TOTAL[0]))
    if FAILED:
        print("실패:", FAILED)
        sys.exit(1)


if __name__ == "__main__":
    main()
