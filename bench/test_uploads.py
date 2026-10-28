#------------------------------------------------------------------
# 업로드 저장소 시험 — index/uploads.py (문서 업로드 설계서 §12 ①)
#=> 모델 없이 가짜 임베더·가짜 추출로 저장소만 본다.
#    1) 올리기 — meta · chunks.jsonl · vectors.f32 · 사본, 청크 id 이어 붙이기
#    2) 같은 이름 두 번 — 사본 이름이 겹치지 않는다
#    3) 빼기 — id 를 0부터 다시 매기고 벡터도 같이 뺀다 · 마지막 하나면 폴더째 지운다
#    4) 코퍼스 — 전수 내적 순서 · BM25 · payload(사본 경로 · 폴더 "업로드")
#    5) 설정이 바뀌면 compatible 이 False · 지원하지 않는 형식 · 대화 id 거르기
#   실제 문서로 두 판(Python · Rust) 근거가 같은지는 CLI 로 쟀다(설계서 §12 ②).
#
#   사용: .venv\Scripts\python bench/test_uploads.py
#------------------------------------------------------------------
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np  # noqa: E402

from simplerag import config  # noqa: E402
from simplerag.index import uploads  # noqa: E402

fail = 0


# 시험 결과 한 줄
def check(name, ok):
    global fail
    fail += not ok
    print("{} {}".format("OK  " if ok else "FAIL", name))


class FakeEmbedder:
    # 글자 수로 만든 정규화 벡터(같은 글이면 같은 벡터)
    tokenizer = None

    def embed_passages(self, texts, on_batch=None):
        out = np.zeros((len(texts), config.EMBED.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, len(t) % config.EMBED.dim] = 1.0
        if on_batch:
            on_batch(len(texts), len(texts))
        return out


with tempfile.TemporaryDirectory() as root:
    config.ROOT = root
    # 추출은 파일 글을 줄마다 청크로 — 실제 추출기 없이
    uploads.extract_chunks = lambda path, name, ext, emb: [
        {"text": l.strip(), "section_id": None} for l in open(path, encoding="utf-8") if l.strip()]
    # BM25 토크나이저 — 시험용(실제는 임베더의 토크나이저)
    from simplerag.index import bm25 as bm25mod
    bm25mod.tokenize = lambda text, tok, mode=None: text.split()
    emb = FakeEmbedder()
    src = os.path.join(root, "src")
    os.makedirs(src)
    a = os.path.join(src, "계약서.txt")
    b = os.path.join(src, "지시서.txt")
    open(a, "w", encoding="utf-8").write("침구류 한도 25만원\n결제는 법인카드\n")
    open(b, "w", encoding="utf-8").write("과업 기간 3개월\n")

    r1 = uploads.add("1793.5", a, emb, ext=object())
    r2 = uploads.add("1793.5", b, emb, ext=object())
    r3 = uploads.add("1793.5", a, emb, ext=object())
    d = uploads.upload_dir("1793.5")
    meta = uploads.read_meta("1793.5")
    rows, vecs = uploads._read_rows(d)
    check("올리기 세 번 — 청크 2+1+2, id 0~4 이어 붙음", [r["chunks"] for r in (r1, r2, r3)] == [2, 1, 2]
          and [r["id"] for r in rows] == [0, 1, 2, 3, 4] and vecs.shape == (5, config.EMBED.dim))
    check("같은 이름 두 번 — 사본 이름이 겹치지 않는다",
          len({f["stored"] for f in meta["files"]}) == 3 and all(os.path.isfile(os.path.join(d, f["stored"])) for f in meta["files"]))
    check("meta 가 지금 설정과 맞다(compatible)", uploads.compatible(meta))

    c = uploads.corpus("1793.5", emb)
    hit = c.search(emb.embed_passages(["침구류 한도 25만원"])[0], 2)
    check("전수 내적 — 같은 글의 청크가 1위, payload 는 사본 경로 · 폴더 '업로드'",
          hit[0][2]["text"] == "침구류 한도 25만원" and hit[0][2]["folder"] == uploads.FOLDER_LABEL
          and hit[0][2]["doc_path"].startswith(os.path.normpath(d)))
    check("BM25 — '과업' 은 지시서 청크", c.bm25.search("과업", None, 3) == [2])

    rr = uploads.remove("1793.5", "지시서.txt")
    rows, vecs = uploads._read_rows(d)
    meta = uploads.read_meta("1793.5")
    check("빼기 — 남은 청크 4개 id 0~3 · 벡터도 4개 · 파일 2개", rr["ok"] and [r["id"] for r in rows] == [0, 1, 2, 3]
          and len(vecs) == 4 and [f["name"] for f in meta["files"]] == ["계약서.txt", "계약서.txt"]
          and [r["file"] for r in rows] == [0, 0, 1, 1])
    uploads.remove("1793.5", "계약서.txt")
    uploads.remove("1793.5", "계약서.txt")
    check("마지막 문서를 빼면 폴더째 지운다", not os.path.exists(d) and uploads.corpus("1793.5", emb) is None)

    meta2 = dict(uploads._new_meta("x"), embed_model="다른모델")
    check("임베딩 모델이 다르면 compatible False", not uploads.compatible(meta2))
    png = os.path.join(src, "사진.png")
    open(png, "wb").write(b"x")
    check("지원하지 않는 형식", not uploads.add("1793.6", png, emb)["ok"])
    check("대화 id 거르기 — 경로를 벗어나는 글자", uploads.safe_id("..\\..\\x") == "x" and uploads.safe_id("") is None)

print("실패 {}건".format(fail))
sys.exit(1 if fail else 0)
