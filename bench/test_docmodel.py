#------------------------------------------------------------------
# 구조화 추출(블록 모델) 회귀 테스트 (plan/구조화추출_청킹_재설계서.html Phase 1)
#=> 세 가지를 확인한다.
#     1) 회귀 0 — 구조 파서가 없는 포맷(DOC·TXT 등)은 blocks 엔진에서도 기존
#        split_structured 와 **글자까지 같은 청크**를 낸다(§11 회귀 기준)
#     2) 부품 규칙 — 병합 셀 펼치기, Markdown/HTML 표, 정규식 제목 폴백(부칙 아래 조문),
#        목차 표시, 무손실 폴백, 접두 줄이기, 표 조각 머리글 재부착
#     3) 글자 보존 — sample 폴더의 블록 경로 문서마다 기존 추출기 대비 본문 글자 비율
#        (너무 적으면 파서가 무언가를 놓친 것)
#
#   실행: .venv/Scripts/python.exe bench/test_docmodel.py
#------------------------------------------------------------------

import os
import re
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                                   # noqa: E402
from simplerag.chunk import split_blocks, split_structured       # noqa: E402
from simplerag.docmodel import blocks as B                       # noqa: E402
from simplerag.docmodel import extract_document, postprocess, supports, tables  # noqa: E402
from simplerag.docmodel.assemble import build_sections, make_prefix  # noqa: E402
from simplerag.docmodel.html_md import _html_grid, md_blocks     # noqa: E402

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample")
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
# 표 격자 — 병합 펼치기 규칙
#=> 세로 병합은 아래 행에 같은 글자를 되풀이하고, 가로 병합은 첫 칸만 채운다.
#   빈 열은 걷어 낸다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_tables():
    print("[표 격자]")
    g = tables.new_grid(3, 3)
    tables.place(g, 0, 0, "구분")
    tables.place(g, 0, 1, "금액", colspan=2)
    tables.place(g, 1, 0, "본인", rowspan=2)
    tables.place(g, 1, 1, "결혼")
    tables.place(g, 1, 2, "500,000")
    tables.place(g, 2, 1, "회갑")
    tables.place(g, 2, 2, "300,000")
    rows = tables.finish(g)
    check("세로 병합은 되풀이", rows[2][0] == "본인", rows)
    check("가로 병합은 첫 칸만", rows[0] == ["구분", "금액", ""], rows)
    check("머리글 추정(숫자 없는 첫 행)", tables.header_rows(rows) == 1)
    check("값 행은 머리글 아님", tables.header_rows(rows[1:]) == 0)
    check("빈 열 제거", tables.finish([["a", "", "b"], ["c", None, "d"]]) == [["a", "b"], ["c", "d"]])


#------------------------------------------------------------------
# XLSX — 한 시트에 옆으로 붙은 표 나누기
#=> 모든 행이 빈 열을 경계로 표를 나누고, 2열 미만 구간(여백 칸)은 이웃 표에 붙인다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_xlsx_split():
    from xml.etree import ElementTree as ET
    from simplerag.docmodel.ooxml_xlsx import _sheet_tables

    # 표 1(A~B) · 빈 C 열 · 표 2(D~E) · 빈 F 열 · 한 칸짜리 G 열(여백 주석)
    cells = {"A1": "구분", "B1": "금액", "D1": "항목", "E1": "기한",
             "A2": "결혼", "B2": "500,000", "D2": "신청", "E2": "7일", "G2": "※"}
    rows = {}
    for ref, v in cells.items():
        rows.setdefault(ref[1:], []).append('<c r="{}" t="inlineStr"><is><t>{}</t></is></c>'.format(ref, v))
    xml = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
           + "".join('<row r="{}">{}</row>'.format(r, "".join(cs)) for r, cs in sorted(rows.items()))
           + "</sheetData></worksheet>")
    got = _sheet_tables(ET.fromstring(xml), [], [], [], "t")
    print("[XLSX 옆 표 나누기]")
    check("빈 열로 표 2개", len(got) == 2, got)
    check("첫 표", got and got[0] == [["구분", "금액"], ["결혼", "500,000"]], got[:1])
    check("한 칸 여백 열은 이웃 표에 붙음", len(got) == 2 and got[1][1][-1] == "※", got[1:])


#------------------------------------------------------------------
# DOC — 필드 명령 제거 · PDF — 머리말/쪽번호 판정 (Phase 2)
#=> DOC 는 0x13 명령 0x14 결과 0x15 중 명령만 버리는지, PDF 는 3쪽 이상 되풀이되는 가장자리 줄을
#   머리말로 표시하되 처음 1개는 본문에 남기는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_doc_pdf_rules():
    from simplerag.docmodel.doc_bin import _Walker
    from simplerag.docmodel.pdf import _edge_kinds

    print("[DOC 필드 · PDF 가장자리]")
    text = "목차\x13 TOC \\o \"1-3\" \x14제1조 목적\x15\r본문\x07셀\x07\x07"
    w = _Walker(text, [(0, len(text), 0, True, text)], [(0, 10 ** 6, {"istd": 0})], [])
    paras = [(t, m) for t, m, _ in w.paragraphs((0, len(text)))]
    check("필드 명령은 버리고 결과만", paras[0] == ("목차제1조 목적", "\r"), paras[:1])
    check("셀 끝 표시로 문단 끊기", [m for _, m in paras] == ["\r", "\x07", "\x07", "\x07"], paras)

    # 쪽마다: 머리말 · 숫자만 다른 짧은 조문 제목 · 본문 여러 줄 · 쪽번호
    words = ["출장", "휴가", "경조", "자산", "보안", "교육", "평가", "급여", "복무", "징계",
             "안전", "점검", "계약", "구매", "회계", "감사", "인사", "채용", "퇴직", "포상"]
    # 실제 본문처럼 쪽마다 다른 문장(숫자만 다른 같은 문장이면 꼬리말로 보는 것이 맞다)
    pages = [["회사 내부 관리 지침 · {}쪽".format(i), "제{}조".format(i)]
             + ["{} 관련 사항은 담당 부서가 처리한다.".format(words[(i - 1) * 4 + k]) for k in range(4)]
             + ["- {} -".format(i)] for i in range(1, 6)]
    kinds = _edge_kinds(pages)
    check("되풀이 머리말(첫 쪽 제외 4개)", sum(1 for k in kinds.values() if k == "header") == 4, kinds)
    check("첫 쪽 머리말은 본문으로 남김", (1, 0) not in kinds)
    check("쪽번호 줄", all(kinds.get((p, 6)) == "page_number" for p in range(1, 6)))
    check("숫자만 다른 짧은 줄(제N조)은 머리말 아님", not any(kinds.get((p, 1)) for p in range(1, 6)), kinds)
    check("본문 줄은 표시 안 함", not any(kinds.get((p, j)) for p in range(1, 6) for j in range(2, 6)))


#------------------------------------------------------------------
# Markdown · HTML 블록
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_md_html():
    print("[Markdown · HTML]")
    bs = md_blocks("# 제목\n\n본문 한 줄\n둘째 줄\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nx = 1\n```\n- 항목")
    kinds = [b.type for b in bs]
    check("MD 종류 순서", kinds == ["heading", "paragraph", "table", "code", "list_item"], kinds)
    tbl = next(b for b in bs if b.type == "table")
    check("MD 표 머리글 1행", tbl.header_rows == 1 and tbl.rows == [["a", "b"], ["1", "2"]], tbl.rows)
    rows, nh = _html_grid([[("구분", 1, 1, True), ("값", 1, 1, True)],
                           [("가", 2, 1, False), ("1", 1, 1, False)],
                           [("2", 1, 1, False)]])
    check("HTML rowspan 자리 건너뛰기", rows == [["구분", "값"], ["가", "1"], ["가", "2"]], rows)
    check("HTML th 행 = 머리글", nh == 1)

    # </head> 생략 · 본문이 <svg><foreignObject> 안에 든 슬라이드 HTML(Marp) — 본문을 잃으면 안 된다
    import tempfile
    from simplerag.docmodel.html_md import extract_html
    html = ("<html><head><title>제안 비교표</title><style>p{}</style>"
            "<body><svg viewBox='0 0 10 10'><foreignObject><section><h2>Q3. 경로 길이 제한</h2>"
            "<p>자사는 260자 제한을 우회한다.</p></section></foreignObject></svg></body></html>")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
    texts = [b.text for b in extract_html(f.name).blocks]
    os.unlink(f.name)
    check("HTML <title> 살림", "제안 비교표" in texts, texts)
    check("HTML svg 속 본문 살림", "자사는 260자 제한을 우회한다." in texts, texts)


#------------------------------------------------------------------
# 후처리 — 정규식 제목 폴백 · 목차 · 무손실 폴백
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_postprocess():
    print("[후처리]")
    texts = ["취업규칙", "제1조(목적) 이 규칙은 …", "제1조(목적)", "본문 1", "제2조(정의)", "본문 2",
             "제3조(적용)", "본문 3", "부 칙", "제1조(시행일)", "이 규칙은 공포한 날부터 시행한다."]
    doc = B.Document("docx", "t", blocks=[B.Block(B.PARAGRAPH, t) for t in texts])
    postprocess.finalize(doc)
    heads = [(b.text, b.level) for b in doc.blocks if b.type == "heading"]
    check("조문 제목 폴백", ("제2조(정의)", 2) in heads, heads)
    check("부칙은 최상위", ("부 칙", 1) in heads, heads)
    secs = build_sections(doc)
    last = secs[-1].path
    check("부칙 아래 조문 경로", last == ["부 칙", "제1조(시행일)"], last)

    toc = ["제1장 총칙 ········ 3", "제2장 근무 ········ 7", "제3장 급여 ········ 12"]
    doc = B.Document("docx", "t", blocks=[B.Block(B.PARAGRAPH, t) for t in toc]
                     + [B.Block(B.PARAGRAPH, "본문 내용이 충분히 깁니다. " * 20)])
    postprocess.finalize(doc)
    check("목차 표시", [b.type for b in doc.blocks][:3] == ["toc"] * 3)
    check("목차 있어도 블록 경로 유지", doc.is_blocks)

    doc = B.Document("docx", "t", blocks=[B.Block(B.HEADER, "머리말 " * 50),
                                          B.Block(B.PARAGRAPH, "짧은 본문")])
    postprocess.finalize(doc)
    check("제외가 70% 넘으면 평문 폴백", not doc.is_blocks and "짧은 본문" in doc.raw_text)

    doc = B.Document("docx", "t", blocks=[B.Block(B.TABLE, rows=[["상자 첫 줄\n상자 둘째 줄"]])])
    postprocess.finalize(doc)
    check("한 칸 표는 문단으로", [b.text for b in doc.blocks] == ["상자 첫 줄", "상자 둘째 줄"])

    # DOC 의 눕힌 행 — 한 칸에 여러 행을 줄바꿈으로 넣은 표(경조사지원규정 형태)
    stacked = [["구 분", "내 역", "금액(원)"],
               ["경 사", "본인 결혼\n자녀 결혼\n부모 회갑", "500,000\n300,000\n200,000"]]
    doc = B.Document("doc", "t", blocks=[B.Block(B.TABLE, rows=[list(r) for r in stacked], header_rows=1)])
    postprocess.finalize(doc)
    rows = doc.blocks[0].rows
    check("DOC 눕힌 행 펼치기", rows[1:] == [["경 사", "본인 결혼", "500,000"], ["경 사", "자녀 결혼", "300,000"],
                                       ["경 사", "부모 회갑", "200,000"]], rows)
    doc = B.Document("docx", "t", blocks=[B.Block(B.TABLE, rows=[list(r) for r in stacked], header_rows=1)])
    postprocess.finalize(doc)
    check("DOCX 는 펼치지 않음(긴 설명 칸 보호)", len(doc.blocks[0].rows) == 2)


#------------------------------------------------------------------
# 접두 줄이기
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_prefix():
    print("[접두]")
    check("짧은 경로 그대로", make_prefix("문서", ["장", "조"]) == "[문서 > 장 > 조]\n")
    p = make_prefix("문서", ["아주 긴 장 제목 " * 3, "더 긴 절 제목 " * 3, "마지막 조"], max_chars=40)
    check("긴 경로는 가운데 생략", p.startswith("[문서 > …") and p.rstrip().endswith("마지막 조]"), p)


#------------------------------------------------------------------
# 청킹 — 표 머리글 재부착 · 폴백 회귀 0
#
# -in: tok = 토크나이저
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_chunking(tok):
    print("[청킹]")
    rows = [["구분", "내역", "금액(원)"]] + [["경조", "항목 {}".format(i), "{},000".format(i)]
                                          for i in range(60)]
    doc = B.Document("docx", "표문서", blocks=[B.Block(B.HEADING, "제5조(지급 금액)", level=1),
                                            B.Block(B.TABLE, rows=rows, header_rows=1)])
    postprocess.finalize(doc)
    chunks = split_blocks(doc, tok)
    check("큰 표는 여러 조각", len(chunks) > 1, len(chunks))
    check("모든 조각에 머리글", all("| 구분 | 내역 | 금액(원) |" in c["text"] for c in chunks),
          [c["text"][:60] for c in chunks if "| 구분" not in c["text"]][:2])
    check("조각마다 경로 접두", all(c["text"].startswith("[표문서 > 제5조(지급 금액)]") for c in chunks))
    check("절 번호 메타", all(c["section_id"] == 0 for c in chunks))
    check("예산(+머리글) 이내", max(_n(tok, c["text"]) for c in chunks)
          <= config.CHUNK_TOKENS + config.TABLE_HEADER_MAX_TOKENS)

    # 표 두 개가 이어질 때 — 다음 표 머리글이 앞 조각 끝에 홀로 남지 않아야 한다
    t1 = [["항목", "값"]] + [["가{}".format(i), "{}".format(i)] for i in range(40)]
    t2 = [["구분", "기한"]] + [["나{}".format(i), "{}일".format(i)] for i in range(40)]
    doc = B.Document("xlsx", "두표", blocks=[B.Block(B.HEADING, "시트", level=1, synthetic=True),
                                          B.Block(B.TABLE, rows=t1, header_rows=1),
                                          B.Block(B.TABLE, rows=t2, header_rows=1)])
    postprocess.finalize(doc)
    texts = [c["text"] for c in split_blocks(doc, tok)]
    tail_head = [t for t in texts if t.rstrip().endswith("| 구분 | 기한 |")]
    check("다음 표 머리글이 조각 끝에 홀로 남지 않음", not tail_head, tail_head[:1])
    check("머리글 중복 없음", all(t.count("| 구분 | 기한 |") <= 1 for t in texts))

    # 폴백 문서는 split_structured 와 글자까지 같아야 한다
    text = "제1조(목적)\n이 규정은 …\n제2조(정의)\n" + "용어의 뜻은 다음과 같다. " * 40 + "\n제3조(적용)\n끝"
    doc = B.Document("legacy", "규정", raw_text=text)
    a = [c["text"] for c in split_blocks(doc, tok)]
    b = split_structured(text, tok, title="규정")
    check("폴백 = split_structured (합성 문서)", a == b)


#------------------------------------------------------------------
# 토큰 길이
#
# -in: tok, s
#
# -out: int
# -out: error = 없음
#------------------------------------------------------------------
def _n(tok, s):
    return len(tok.encode(s, add_special_tokens=False).ids)


#------------------------------------------------------------------
# sample 폴더 — 폴백 회귀 0 · 블록 경로 글자 보존
#=> 블록 경로 문서는 기존 추출기 대비 '공백 뺀 글자 수' 비율을 본다. 머리말·꼬리말은
#   양쪽 다 들어가므로 비율이 1 근처여야 하고, 0.9 아래면 파서가 무언가를 놓친 것이다.
#   (블록 쪽이 각주·표 머리글 등을 더 읽어 1 을 넘는 것은 정상)
#
# -in: tok = 토크나이저
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_samples(tok):
    print("[sample 폴더]")
    from simplerag.extract import build_extractor, extract_text

    if not os.path.isdir(SAMPLE):
        print("  (sample 폴더 없음 — 건너뜀)")
        return
    ext = build_extractor()
    # 기존 추출기 — 비교 기준이자 파서 없는 포맷의 폴백
    legacy = (lambda p: extract_text(ext, p))
    # 공백을 뺀 글자 수 — 줄바꿈·들여쓰기 차이는 세지 않는다
    size = (lambda s: len(re.sub(r"\s+", "", s or "")))
    for f in sorted(os.listdir(SAMPLE)):
        p = os.path.join(SAMPLE, f)
        if not os.path.isfile(p):
            continue
        try:
            old_text, _ = legacy(p)
        except Exception as e:
            print("  --   기존 추출 실패, 건너뜀: {} ({})".format(f, type(e).__name__))
            continue
        doc = extract_document(p, legacy=legacy)
        if not supports(p):
            # 파서 없는 포맷 — 기존 청킹과 글자까지 같아야 한다
            a = [c["text"] for c in split_blocks(doc, tok)]
            b = split_structured(old_text, tok, title=os.path.splitext(f)[0])
            check("폴백 회귀 0: {}".format(f), a == b, "{} vs {} 청크".format(len(a), len(b)))
            continue
        # 글자 수 비율은 참고만 한다 — 기존 추출은 글상자 대체본(mc:Fallback)을 두 번 읽거나
        # normalize_tables 가 그룹 이름표를 행마다 되풀이해 부풀려진다(연구소안전관리규정.docx
        # 0.67 · MCCS xlsx 0.31 이 그 경우로, 빠진 낱말은 0개였다). 판정은 '낱말 덮음률'로 한다.
        ratio = size(doc.full_text()) / max(1, size(old_text))
        old_w, new_w = _words(old_text), _words(doc.full_text())
        cover = len(old_w & new_w) / max(1, len(old_w))
        chunks = split_blocks(doc, tok)
        check("낱말 덮음 {:.3f} (글자비 {:.2f}) · 청크 {:>3} · {}".format(cover, ratio, len(chunks), f),
              doc.is_blocks and cover >= 0.98,
              sorted(old_w - new_w)[:15] or doc.warnings)


#------------------------------------------------------------------
# 낱말 집합 — 한글·영문·숫자 2자 이상
#
# -in: s = 글
#
# -out: set[str]
# -out: error = 없음
#------------------------------------------------------------------
def _words(s):
    return set(re.findall(r"[가-힣A-Za-z0-9]{2,}", s or ""))


if __name__ == "__main__":
    from simplerag.embed.onnx_embedder import OnnxEmbedder

    test_tables()
    test_xlsx_split()
    test_doc_pdf_rules()
    test_md_html()
    test_postprocess()
    test_prefix()
    emb = OnnxEmbedder()
    emb.ensure_loaded()
    test_chunking(emb.tokenizer)
    test_samples(emb.tokenizer)
    print("\n{}/{} 통과".format(TOTAL[0] - len(FAILED), TOTAL[0]))
    if FAILED:
        print("실패: " + ", ".join(FAILED))
        sys.exit(1)
