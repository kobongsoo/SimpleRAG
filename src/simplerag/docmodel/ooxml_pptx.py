#------------------------------------------------------------------
# PPTX → 블록 (재설계서 §7)
#=> 발표자료는 **슬라이드가 곧 절**이다.
#    · 슬라이드 순서 — presentation.xml 의 슬라이드 목록(rels)을 따른다(파일 번호 아님)
#    · 제목 — 제목 자리표시자(type="title"/"ctrTitle") 글자 = 수준 1 제목.
#             제목이 없는 슬라이드는 합성 제목 "슬라이드 N"(본문에는 넣지 않음)
#    · 본문 — 도형 글상자 문단. 들여쓰기 수준(lvl) > 0 이면 목록 항목
#    · 표   — <a:tbl> → 행렬(gridSpan·rowSpan·hMerge·vMerge 펼침), 첫 행 머리글 추정
#    · 슬라이드 번호·날짜·바닥글 자리표시자 → 제외 블록
#    · 발표자 노트 — 슬라이드 rels 로 짝을 찾아 문서 끝 '발표자 노트' 절로 모은다
#   이식 출처: MpowerClassify office_ooxml.py PptxExtractor (노트까지 읽는 이유 동일 —
#   노트 2,000자를 놓쳐 재현율 24.9% 였던 문서가 있었다)
#------------------------------------------------------------------

import re
import zipfile

from . import blocks as B
from . import tables
from .ooxml_common import attr, child, descendants, local, num_key, parse, rels

_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml$", re.I)
_TITLE_PH = ("title", "ctrTitle")
_FOOT_PH = {"sldNum": B.PAGE_NUMBER, "dt": B.FOOTER, "ftr": B.FOOTER, "hdr": B.HEADER}


#------------------------------------------------------------------
# PPTX 추출 (진입점)
#
# -in: path  = .pptx 경로
# -in: title = 문서 제목
#
# -out: Document (블록 경로, 후처리 전). pages = 슬라이드 수
# -out: error = ZIP 아님·슬라이드 없음이면 ValueError (상위가 legacy 로 폴백)
#------------------------------------------------------------------
def extract(path, title=""):
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError("pptx 열기 실패: {}".format(type(e).__name__))
    with z:
        slides = _slide_order(z)
        if not slides:
            raise ValueError("pptx 슬라이드 없음")
        doc = B.Document(format="pptx", title=title, pages=len(slides))
        notes = []
        for no, part in enumerate(slides, 1):
            root = parse(z, part)
            if root is None:
                doc.warnings.append("슬라이드 {} XML 손상".format(no))
                continue
            _slide(root, no, doc.blocks)
            # 노트는 rels 로 짝지어진 notesSlide 에 있다
            for kind, target in rels(z, part).values():
                if kind == "notesSlide":
                    nroot = parse(z, target)
                    texts = _note_texts(nroot) if nroot is not None else []
                    if texts:
                        notes.append((no, texts))
        if notes:
            doc.blocks.append(B.Block(B.HEADING, "발표자 노트", level=1, synthetic=True))
            for no, texts in notes:
                doc.blocks.append(B.Block(B.HEADING, "슬라이드 {}".format(no), level=2,
                                          page=no, synthetic=True))
                doc.blocks.extend(B.Block(B.NOTE, t, page=no) for t in texts)
    return doc


#------------------------------------------------------------------
# 슬라이드 순서 정하기
#=> presentation.xml 의 <p:sldIdLst> 순서가 발표 순서다. 슬라이드를 옮기면 파일
#   번호와 발표 순서가 달라지므로 rels 로 실제 파일을 찾는다. 실패하면 파일 번호순.
#
# -in: z = 열린 ZipFile
#
# -out: list[str] = 슬라이드 부품 경로(발표 순서)
# -out: error = 없음
#------------------------------------------------------------------
def _slide_order(z):
    names = set(z.namelist())
    pres = parse(z, "ppt/presentation.xml")
    if pres is not None:
        rmap = rels(z, "ppt/presentation.xml")
        order = []
        for e in descendants(pres, "sldId"):
            # sldId 의 r:id 는 지역명이 'id' 라 숫자 id 속성과 겹친다 — rels 에 있는 값으로 가른다
            rid = next((v for k, v in e.attrib.items()
                        if local(k) == "id" and v in rmap), None)
            if rid and rmap[rid][1] in names:
                order.append(rmap[rid][1])
        if order:
            return order
    return sorted((n for n in names if _SLIDE.search(n)), key=lambda n: num_key(n, _SLIDE))


#------------------------------------------------------------------
# 슬라이드 하나 → 블록
#=> 제목 도형을 먼저 찾아 절 제목으로 세우고, 나머지 도형을 문서 순서대로 낸다.
#
# -in: root = 슬라이드 XML 루트
# -in: no   = 슬라이드 번호(1부터)
# -in: out  = 블록 리스트(제자리)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _slide(root, no, out):
    body = []
    title_text = []
    tree = None
    for e in root.iter():
        if local(e.tag) == "spTree":
            tree = e
            break
    if tree is not None:
        _shapes(tree, no, body, title_text)
    head = " ".join(t for t in title_text if t.strip()).strip()
    if head:
        out.append(B.Block(B.HEADING, head, level=1, page=no))
    else:
        out.append(B.Block(B.HEADING, "슬라이드 {}".format(no), level=1, page=no, synthetic=True))
    out.extend(body)


#------------------------------------------------------------------
# 도형 트리 돌기
#=> 도형(sp)·그룹(grpSp)·표 틀(graphicFrame)을 문서 순서로 처리한다.
#   그림·연결선은 글자가 없어 건너뛴다.
#
# -in: tree       = spTree 또는 grpSp 요소
# -in: no         = 슬라이드 번호
# -in: body       = 본문 블록 리스트(제자리)
# -in: title_text = 제목 글자 리스트(제자리)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _shapes(tree, no, body, title_text):
    for sh in tree:
        ln = local(sh.tag)
        if ln == "grpSp":
            _shapes(sh, no, body, title_text)
        elif ln == "sp":
            ph = None
            for e in sh.iter():
                if local(e.tag) == "ph":
                    ph = attr(e, "type") or "body"
                    break
            paras = _paragraphs(child(sh, "txBody"))
            if not paras:
                continue
            if ph in _TITLE_PH:
                title_text.extend(t for t, _ in paras)
            elif ph in _FOOT_PH:
                body.extend(B.Block(_FOOT_PH[ph], t, page=no) for t, _ in paras)
            else:
                for t, lvl in paras:
                    kind = B.LIST_ITEM if lvl > 0 else B.PARAGRAPH
                    body.append(B.Block(kind, t, level=lvl if lvl > 0 else 0, page=no))
        elif ln == "graphicFrame":
            for tbl in descendants(sh, "tbl"):
                rows = _table(tbl)
                if rows:
                    body.append(B.Block(B.TABLE, rows=rows, page=no))


#------------------------------------------------------------------
# 글상자의 문단들
#=> <a:p> 마다 <a:t> 를 잇고, <a:br/> 은 줄바꿈으로 살린다. 들여쓰기 수준은 <a:pPr lvl>.
#
# -in: txbody = <p:txBody> 또는 <a:txBody> (없으면 None)
#
# -out: [(글자, 수준), …] — 빈 문단 제외
# -out: error = 없음
#------------------------------------------------------------------
def _paragraphs(txbody):
    out = []
    if txbody is None:
        return out
    for p in txbody:
        if local(p.tag) != "p":
            continue
        buf = []
        for e in p.iter():
            ln = local(e.tag)
            if ln == "t":
                buf.append(e.text or "")
            elif ln == "br":
                buf.append("\n")
        s = "".join(buf)
        if s.strip():
            lvl = attr(child(p, "pPr"), "lvl")
            out.append((s, int(lvl) if lvl and lvl.isdigit() else 0))
    return out


#------------------------------------------------------------------
# 슬라이드 표 → 행렬
#=> <a:tc> 의 gridSpan/rowSpan 은 병합 시작, hMerge/vMerge="1" 은 이어진 칸이다.
#   이어진 가로 칸은 비우고, 이어진 세로 칸은 위 칸 글자를 되풀이한다(tables.place 규칙).
#
# -in: tbl = <a:tbl>
#
# -out: rows (내용 없으면 [])
# -out: error = 없음
#------------------------------------------------------------------
def _table(tbl):
    trs = [tr for tr in tbl if local(tr.tag) == "tr"]
    width = max((sum(1 for tc in tr if local(tc.tag) == "tc") for tr in trs), default=0)
    grid = tables.new_grid(len(trs), width)
    for r, tr in enumerate(trs):
        c = 0
        for tc in tr:
            if local(tc.tag) != "tc":
                continue
            text = "\n".join(t for t, _ in _paragraphs(child(tc, "txBody")))
            if attr(tc, "hMerge") in ("1", "true"):
                text = ""
            elif attr(tc, "vMerge") in ("1", "true") and r > 0:
                text = grid[r - 1][c] or ""
            if grid[r][c] is None:
                grid[r][c] = text
            c += 1
    return tables.finish(grid)


#------------------------------------------------------------------
# 노트 슬라이드의 본문 글자
#=> 노트 슬라이드에는 슬라이드 축소 그림·번호 자리표시자도 있다. 본문(body) 자리표시자의
#   문단만 모은다.
#
# -in: root = notesSlide XML 루트
#
# -out: list[str] = 노트 문단
# -out: error = 없음
#------------------------------------------------------------------
def _note_texts(root):
    out = []
    for sh in descendants(root, "sp"):
        ph = None
        for e in sh.iter():
            if local(e.tag) == "ph":
                ph = attr(e, "type") or "body"
                break
        if ph != "body":
            continue
        out.extend(t for t, _ in _paragraphs(child(sh, "txBody")))
    return out
