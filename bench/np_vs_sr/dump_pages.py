"""sample_pdf 의 PDF 30개를 쪽 단위 텍스트로 뽑아 골든셋 작성용 파일과 목록을 만든다."""
import hashlib
import json
import os
import sys

import pypdfium2 as pdfium

SRC = r"D:\Project\SimpleRAG\sample_pdf"
OUT = sys.argv[1]


#------------------------------------------------------------------
# PDF 한 개 → 쪽별 텍스트
#=> pdfium 텍스트층을 쪽마다 읽는다(SimpleRAG PDF 추출기와 같은 엔진).
#
# -in: path = PDF 경로
#
# -out: pages = 쪽별 문자열 목록 (1쪽이 0번)
# -out: error = 열 수 없는 PDF 면 pdfium 예외 전파
#------------------------------------------------------------------
def pages_of(path):
    pdf = pdfium.PdfDocument(path)
    try:
        return [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    finally:
        pdf.close()


#------------------------------------------------------------------
# 전체 덤프
#=> 내용이 같은 사본은 한 문서로 묶고(sha1), 문서마다 "=== p.N ===" 표지를 넣은 txt 를 쓴다.
#
# -in: 없음
#
# -out: 없음 (OUT/pages/*.txt, OUT/docs.json)
# -out: error = 예외 전파
#------------------------------------------------------------------
def main():
    os.makedirs(os.path.join(OUT, "pages"), exist_ok=True)
    docs, by_hash = [], {}
    for name in sorted(os.listdir(SRC)):
        if not name.lower().endswith(".pdf"):
            continue
        pages = pages_of(os.path.join(SRC, name))
        body = "\n".join(pages)
        h = hashlib.sha1(" ".join(body.split()).encode("utf-8")).hexdigest()
        # 사본(내용 동일)은 앞 문서에 파일명만 덧붙인다 — 문항을 두 벌 만들지 않게
        if h in by_hash:
            by_hash[h]["files"].append(name)
            continue
        did = "p{:02d}".format(len(docs) + 1)
        txt = "".join("=== p.{} ===\n{}\n".format(i + 1, t.strip()) for i, t in enumerate(pages))
        with open(os.path.join(OUT, "pages", did + ".txt"), "w", encoding="utf-8") as f:
            f.write(txt)
        d = {"doc_id": did, "files": [name], "pages": len(pages), "chars": len(body)}
        docs.append(d)
        by_hash[h] = d
    json.dump(docs, open(os.path.join(OUT, "docs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for d in docs:
        print(d["doc_id"], d["pages"], d["chars"], " / ".join(d["files"]))


if __name__ == "__main__":
    main()
