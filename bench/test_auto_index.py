#------------------------------------------------------------------
# 자동 인덱싱 AI1 회귀 테스트 (plan/자동인덱싱_설계서.html §5·§6·§7)
#=> 폴더와 상태 파일을 대조해 추가·수정·삭제를 반영하는 index_folder 를 확인한다.
#   특히 "수정 문서는 옛 청크를 지우고 새 청크를 넣는다" 가 어느 단계에서 끊겨도
#   두 벌이 남거나 문서가 사라지지 않는지 본다.
#
#   모델을 올리지 않는다: 임베딩은 글자에서 만든 가짜 벡터(4차원), 토크나이저만 진짜를 쓴다.
#   Qdrant·BM25·상태 파일은 전부 임시 폴더에 만든다 — 운영 인덱스를 건드리지 않는다.
#
#   실행: .venv/Scripts/python.exe bench/test_auto_index.py
#------------------------------------------------------------------

import hashlib
import os
import shutil
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                                    # noqa: E402
from simplerag.index import indexer                             # noqa: E402
from simplerag.index.bm25 import Bm25Index                      # noqa: E402
from simplerag.index.store import COLLECTION, VectorStore       # noqa: E402

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
# 가짜 임베더 — 모델 없이 인덱서를 돌리기 위한 것
#=> 토크나이저는 진짜(tokenizer.json)를 써야 청킹이 운영과 같게 나온다.
#   벡터는 글자 해시로 만든 4차원 값이면 충분하다(검색 품질을 보는 시험이 아니다).
#
# -필드: tokenizer = tokenizers.Tokenizer (절단 해제)
#------------------------------------------------------------------
class FakeEmbedder:
    #--------------------------------------------------------------
    # 생성자 — 운영과 같은 토크나이저 파일을 연다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = tokenizer.json 이 없으면 예외 전파
    #--------------------------------------------------------------
    def __init__(self):
        from tokenizers import Tokenizer
        self.tokenizer = Tokenizer.from_file(os.path.join(config.EMBED.model_dir, "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.fail_on = None          # 이 글자가 든 문서는 임베딩에서 실패시킨다

    def ensure_loaded(self):
        return 0.0

    #--------------------------------------------------------------
    # 글 하나 → 4차원 벡터
    #
    # -in: text = 글
    #
    # -out: numpy 배열 (4,)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _vec(self, text):
        d = hashlib.sha1(text.encode("utf-8")).digest()
        v = np.frombuffer(d[:16], dtype=np.uint32).astype(np.float32) + 1.0
        return v / np.linalg.norm(v)

    def embed_passages(self, chunks, batch=None, on_batch=None):
        if self.fail_on and any(self.fail_on in c for c in chunks):
            raise RuntimeError("시험용 임베딩 실패")
        return np.stack([self._vec(c) for c in chunks])

    def embed_query(self, query):
        return self._vec(query)


#------------------------------------------------------------------
# 문서 쓰기 — 여러 청크가 나오게 길이를 맞춘다
#
# -in: path   = 파일 경로
# -in: marker = 이 문서 버전을 알아볼 낱말
# -in: paras  = 문단 수(많을수록 청크가 늘어난다)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def write_doc(path, marker, paras=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = []
    for i in range(paras):
        body.append("{} 제{}조 출장 여비는 규정에 따라 지급한다. ".format(marker, i + 1) * 60)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(body))


#------------------------------------------------------------------
# 파일 수정 시각을 확실히 바꾸기
#=> 지문은 초 단위 mtime 을 쓴다. 같은 초 안에 다시 쓰면 "안 바뀜" 으로 보이므로
#   시험에서는 시각을 직접 앞으로 민다.
#
# -in: path = 파일 경로
# -in: sec  = 밀 초
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def bump(path, sec=10):
    st = os.stat(path)
    os.utime(path, (st.st_atime + sec, st.st_mtime + sec))


#------------------------------------------------------------------
# 저장소의 점을 문서별로 모으기
#
# -in: store = VectorStore
#
# -out: {doc_path: [(id, payload), …]}
# -out: error = 없음
#------------------------------------------------------------------
def points_by_doc(store):
    out, offset = {}, None
    while True:
        pts, offset = store._client.scroll(collection_name=COLLECTION, limit=2000, offset=offset,
                                           with_payload=True, with_vectors=False)
        for p in pts:
            out.setdefault(p.payload.get("doc_path"), []).append((p.id, p.payload))
        if offset is None:
            return out


def run(root, emb, store, bm25, **kw):
    logs = []
    s = indexer.index_folder(root, emb, store, bm25, on_log=logs.append, **kw)
    s["_log"] = logs
    return s


def main():
    tmp = tempfile.mkdtemp(prefix="simplerag_auto_")
    root = os.path.join(tmp, "docs")
    other = os.path.join(tmp, "docs2")          # 이름이 겹치는 다른 폴더(접두어 함정)
    # 상태 파일·BM25 를 임시 폴더로 돌린다 — 운영 파일을 절대 건드리지 않게
    config.STATE_PATH = os.path.join(tmp, "index_state.json")
    emb = FakeEmbedder()
    store = VectorStore(path=os.path.join(tmp, "qdrant"), dim=4)
    bm25 = Bm25Index(path=os.path.join(tmp, "bm25.npz"))
    A, B, C = (os.path.join(root, n) for n in ("a.txt", "b.txt", "sub\\c.txt"))
    try:
        # ── 1. 처음 인덱싱 ─────────────────────────────
        print("\n[1. 처음 인덱싱]")
        write_doc(A, "사과", paras=3)
        write_doc(B, "바나나")
        write_doc(C, "체리")
        write_doc(os.path.join(root, "~$a.txt"), "임시")         # Office 표시 파일
        write_doc(os.path.join(other, "d.txt"), "대추")
        s = run(root, emb, store, bm25)
        check("추가 3건(임시 파일 제외)", s["added"] == 3, s)
        by = points_by_doc(store)
        check("세 문서 모두 점이 있다", all(os.path.abspath(p) in by for p in (A, B, C)))
        st = indexer.load_state()
        a_chunks = st["docs"][os.path.abspath(A)]["chunks"]
        check("문서 A 는 여러 청크", a_chunks >= 2, a_chunks)
        check("상태 파일에 id 범위·내용 해시", "ids" in st["docs"][os.path.abspath(A)]
              and "content_hash" in st["docs"][os.path.abspath(A)])
        check("청크에 gen(문서 버전) 표시", all(pl.get("gen") for _, pl in by[os.path.abspath(A)]))
        check("BM25 를 만들었다", st.get("bm25_built_at") and not indexer.bm25_stale(st))
        run(other, emb, store, bm25)                              # 다른 폴더도 인덱싱

        # ── 2. 수정 — 옛 청크는 사라지고 새 청크만 ─────
        print("\n[2. 수정: 3청크 → 1청크]")
        write_doc(A, "살구", paras=1)
        bump(A)
        s = run(root, emb, store, bm25)
        by = points_by_doc(store)
        a_pts = by.get(os.path.abspath(A), [])
        new_n = indexer.load_state()["docs"][os.path.abspath(A)]["chunks"]
        check("수정 1건", s["modified"] == 1 and s["added"] == 0, s)
        check("A 의 점 수 = 새 청크 수(옛 청크가 안 남음)", len(a_pts) == new_n, (len(a_pts), new_n))
        check("A 에 옛 내용(사과)이 없다", not any("사과" in pl["text"] for _, pl in a_pts))
        check("A 에 새 내용(살구)이 있다", any("살구" in pl["text"] for _, pl in a_pts))
        check("다른 문서는 그대로", len(by.get(os.path.abspath(B), [])) >= 1)

        # ── 3. 내용 같음 — 다시 임베딩하지 않는다 ───────
        print("\n[3. 날짜만 바뀜]")
        ids_before = sorted(i for i, _ in points_by_doc(store)[os.path.abspath(B)])
        bump(B)
        s = run(root, emb, store, bm25)
        ids_after = sorted(i for i, _ in points_by_doc(store)[os.path.abspath(B)])
        check("내용 같음 1건", s["same"] == 1 and s["modified"] == 0, s)
        check("B 의 점 id 가 그대로(다시 넣지 않음)", ids_before == ids_after)
        s = run(root, emb, store, bm25)
        check("그다음 실행은 할 일 없음", s["changed"] == 0, s)

        # ── 4. 삭제 ────────────────────────────────────
        print("\n[4. 파일 삭제]")
        os.remove(B)
        s = run(root, emb, store, bm25)
        by = points_by_doc(store)
        check("삭제 1건", s["removed"] == 1, s)
        check("B 의 점이 없다", os.path.abspath(B) not in by)
        check("상태 파일에서도 빠졌다", os.path.abspath(B) not in indexer.load_state()["docs"])
        check("다른 폴더(docs2) 문서는 그대로", os.path.abspath(os.path.join(other, "d.txt")) in by)

        # ── 5. 이름 바꾸기 ──────────────────────────────
        print("\n[5. 이름 변경]")
        C2 = os.path.join(root, "sub", "c2.txt")
        os.rename(C, C2)
        s = run(root, emb, store, bm25)
        by = points_by_doc(store)
        check("삭제 1 + 추가 1", s["removed"] == 1 and s["added"] == 1, s)
        check("새 이름으로만 남았다", os.path.abspath(C2) in by and os.path.abspath(C) not in by)

        # ── 6. 추출 실패 — 옛 버전 유지 ─────────────────
        print("\n[6. 새 버전이 망가짐]")
        emb.fail_on = "망가짐"
        write_doc(A, "망가짐")
        bump(A, 20)
        s = run(root, emb, store, bm25)
        a_pts = points_by_doc(store).get(os.path.abspath(A), [])
        check("실패 1건으로 보고", len(s["failed"]) == 1, s["failed"])
        check("옛 버전(살구)이 그대로 남았다", a_pts and all("살구" in pl["text"] for _, pl in a_pts))
        s = run(root, emb, store, bm25)
        check("안 바뀌었으면 다시 시도하지 않는다", s["changed"] == 0 and not s["failed"], s)
        emb.fail_on = None
        write_doc(A, "자두")
        bump(A, 30)
        s = run(root, emb, store, bm25)
        a_pts = points_by_doc(store).get(os.path.abspath(A), [])
        check("고치면 다시 반영", s["modified"] == 1 and all("자두" in pl["text"] for _, pl in a_pts))
        check("실패 기록이 지워졌다", os.path.abspath(A) not in indexer.load_state().get("failed", {}))

        # ── 7. 넣고 지우기 사이에서 끊김 ─────────────────
        print("\n[7. 새 청크를 넣은 뒤, 옛 청크를 지우기 전에 끊김]")
        write_doc(A, "머루", paras=2)
        bump(A, 40)
        real = store.delete_doc_except
        store.delete_doc_except = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("끊김"))
        try:
            run(root, emb, store, bm25)
            check("끊김이 예외로 올라왔다", False)
        except RuntimeError:
            check("끊김이 예외로 올라왔다", True)
        store.delete_doc_except = real
        a_pts = points_by_doc(store).get(os.path.abspath(A), [])
        check("끊긴 직후엔 두 벌(옛+새)이 있다 — 문서는 사라지지 않았다",
              any("자두" in pl["text"] for _, pl in a_pts) and any("머루" in pl["text"] for _, pl in a_pts))
        s = run(root, emb, store, bm25)
        a_pts = points_by_doc(store).get(os.path.abspath(A), [])
        n = indexer.load_state()["docs"][os.path.abspath(A)]["chunks"]
        check("다시 돌리면 새 버전 한 벌만", len(a_pts) == n and all("머루" in pl["text"] for _, pl in a_pts),
              (len(a_pts), n))

        # ── 8. 같은 버전을 두 번 넣다 끊김 — 세대가 같아도 정리 ──
        print("\n[8. 같은 버전 두 벌]")
        # 7 과 같은 상황을 한 번 더 만들되, 이번엔 상태 파일도 옛 지문으로 되돌려
        # "같은 버전을 두 번 넣은" 모양을 만든다
        # (지문만 되돌리면 "내용 같음" 으로 빠져 다시 넣지 않는다 — 해시도 되돌린다)
        st = indexer.load_state()
        key = os.path.abspath(A)
        st["docs"][key]["fingerprint"] = "옛지문"
        st["docs"][key]["content_hash"] = "옛해시"
        indexer.save_state(st)
        store.delete_doc_except = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("끊김"))
        try:
            run(root, emb, store, bm25)
        except RuntimeError:
            pass
        store.delete_doc_except = real
        a_pts = points_by_doc(store).get(key, [])
        n = indexer.load_state()["docs"][key]["chunks"]
        gens = {pl.get("gen") for _, pl in a_pts}
        check("끊긴 뒤 같은 버전(gen 하나)이 두 벌 있다", len(a_pts) == 2 * n and len(gens) == 1,
              (len(a_pts), n, len(gens)))
        # 상태 파일은 3 단계 전에 끊겨 여전히 옛 지문이다 → 다시 돌리면 다시 처리된다
        s = run(root, emb, store, bm25)
        a_pts = points_by_doc(store).get(key, [])
        check("같은 gen 두 벌도 한 벌로 정리", len(a_pts) == n, (len(a_pts), n))

        # ── 9. 고아 청소 ────────────────────────────────
        print("\n[9. 상태 파일에 없는 점]")
        store.upsert([999999], np.stack([emb._vec("유령")]),
                     [{"text": "유령 문서", "doc_path": os.path.join(root, "ghost.txt")}])
        st = indexer.load_state()
        indexer.mark_changed(st)
        indexer.save_state(st)
        s = run(root, emb, store, bm25)
        check("고아 점을 지웠다", os.path.join(root, "ghost.txt") not in points_by_doc(store),
              [l for l in s["_log"] if "고아" in l])

        # ── 10. 대량 삭제 멈춤 ──────────────────────────
        print("\n[10. 한꺼번에 많이 사라짐]")
        many = [os.path.join(root, "bulk", "m%02d.txt" % i) for i in range(8)]
        for i, p in enumerate(many):
            write_doc(p, "묶음%d" % i)
        run(root, emb, store, bm25)
        for p in many[:6]:
            os.remove(p)
        s = run(root, emb, store, bm25)
        by = points_by_doc(store)
        check("지우지 않고 멈췄다", s["removed"] == 0 and s["delete_blocked"], s["delete_blocked"])
        check("점이 그대로 남았다", all(os.path.abspath(p) in by for p in many[:6]))
        s = run(root, emb, store, bm25, allow_delete=True)
        by = points_by_doc(store)
        check("--allow-delete 면 지운다", s["removed"] == 6 and
              not any(os.path.abspath(p) in by for p in many[:6]), s["removed"])

        # ── 11. 폴더가 안 보이면 아무것도 지우지 않는다 ──
        print("\n[11. 폴더가 사라짐(드라이브 분리)]")
        before = sum(len(v) for v in points_by_doc(store).values())
        gone = root + "_gone"
        os.rename(root, gone)
        try:
            run(root, emb, store, bm25)
            check("폴더 없음은 오류", False)
        except RuntimeError:
            check("폴더 없음은 오류", True)
        after = sum(len(v) for v in points_by_doc(store).values())
        check("점이 하나도 안 지워졌다", before == after, (before, after))
        os.rename(gone, root)

        # ── 12. 지워진 점을 BM25 가 가리켜도 빈 근거가 안 나온다 ──
        print("\n[12. BM25 를 다시 만들기 전 검색]")
        from simplerag.retrieve.hybrid import HybridRetriever
        st = indexer.load_state()
        key = os.path.abspath(A)
        indexer.remove_doc(key, st, store)          # BM25 는 일부러 다시 만들지 않는다
        r = HybridRetriever(emb, store, bm25)
        res = r._search("머루 출장 여비", top_k=5)
        chunks = res[0] if isinstance(res, tuple) else res
        check("근거에 빈 본문이 없다", chunks and all(c["text"] for c in chunks),
              [c.get("text", "")[:10] for c in chunks])
        check("지운 문서가 근거로 안 나온다", not any(c["doc_path"] == key for c in chunks))
        check("BM25 가 오래됐다고 표시", indexer.bm25_stale(indexer.load_state()))
        s = run(root, emb, store, bm25)
        check("다음 실행이 바뀐 게 없어도 BM25 를 다시 만든다",
              any("BM25 인덱스" in l and "구축" in l for l in s["_log"])
              and not indexer.bm25_stale(indexer.load_state()))

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
