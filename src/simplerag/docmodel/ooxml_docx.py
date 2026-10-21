#------------------------------------------------------------------
# DOCX → 블록 (재설계서 §7)
#=> 워드 파일 안에는 "이 문단은 제목 2단계"라는 정답이 들어 있다. 기존 추출기는
#   글자(<w:t>)만 모으고 이를 버렸다. 여기서는 그대로 읽는다.
#    · 제목 — 문단 스타일의 개요 수준(outlineLvl) 또는 스타일 이름(Heading N · 제목 N · 개요 N),
#             basedOn 사슬을 따라 상속값까지 본다. 문단에 직접 지정한 outlineLvl 도 반영
#    · 표   — <w:tbl> → 행렬. gridSpan(가로 병합)·vMerge(세로 병합) 펼침, tblHeader 행 = 머리글
#    · 목록 — numPr 이 있으면 list_item(깊이 = ilvl + 1)
#    · 목차 — TOC 스타일 문단, 목차 문서부품(sdt) → toc (청크에서 제외)
#    · 머리말·꼬리말 — header*.xml · footer*.xml → header/footer (청크에서 제외)
#    · 각주 — footnotes.xml → note (본문 뒤 '각주' 절)
#   이식 출처: MpowerClassify office_ooxml.py DocxExtractor/_para_runs (텍스트상자 중복·
#   탭 정의 오인 방지 규칙을 그대로 따른다)
#------------------------------------------------------------------

import re
import zipfile

from . import blocks as B
from . import tables
from .ooxml_common import attr, child, local, parse

# 스타일 이름으로 제목 수준 읽기 — 영문·한글 워드 기본 이름
_HEADING_NAME = re.compile(r"^(?:heading|제목|개요)\s*(\d)$", re.I)
_TITLE_NAME = re.compile(r"^(?:title|제목)$", re.I)
_TOC_NAME = re.compile(r"^(?:toc|목차)\s*\d*$|^toc heading$", re.I)
_CAPTION_NAME = re.compile(r"^(?:caption|캡션)$", re.I)


#------------------------------------------------------------------
# 문단 하나의 글자 모으기 (런 안에서만, 안쪽 문단은 건너뜀)
#=> MpowerClassify _para_runs 와 같은 규칙이다.
#    · <w:pPr><w:tabs> 안의 <w:tab> 은 탭 정지 '정의'라 무시하고, 런 안의 <w:tab/> 만 탭이다
#    · 텍스트상자(<w:txbxContent>) 안의 문단은 제 줄을 따로 가지므로 여기서 삼키지 않는다
#    · 줄바꿈(<w:br/>, 쪽나눔 제외)·<w:cr/> 은 줄바꿈으로 살린다 — 없애면 두 줄이 붙는다
#
# -in: el     = 훑을 요소(처음에는 문단 <w:p>)
# -in: buf    = 글자 조각을 담을 리스트(제자리에서 늘어난다)
# -in: in_run = 지금 런(<w:r>) 안인가
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _para_runs(el, buf, in_run):
    for ch in el:
        ln = local(ch.tag)
        if ln == "p":
            continue
        # 대체 표현(mc:Fallback)은 Choice 와 같은 글자를 한 번 더 담는다 — 두 번 읽지 않는다
        if ln == "Fallback":
            continue
        if ln == "r":
            _para_runs(ch, buf, True)
            continue
        if in_run and ln == "t":
            buf.append(ch.text or "")
            continue
        if in_run and ln == "tab":
            buf.append("\t")
            continue
        if in_run and ln in ("br", "cr"):
            if attr(ch, "type") != "page":
                buf.append("\n")
            continue
        _para_runs(ch, buf, in_run)


#------------------------------------------------------------------
# 문단 안에 숨은 안쪽 문단(텍스트상자 등) 모으기
#=> 바깥 문단의 글자를 만든 뒤, 그 안의 텍스트상자 문단들을 따로 블록으로 낸다.
#   mc:Fallback 은 같은 글상자의 대체본이라 건너뛴다(같은 글자 두 번 방지).
#
# -in: p = 바깥 문단 요소
#
# -out: list[str] = 안쪽 문단 글자 목록(빈 것 제외)
# -out: error = 없음
#------------------------------------------------------------------
def _inner_paragraphs(p):
    out = []

    # Fallback 아래로는 내려가지 않는 깊이 우선 순회
    def walk(el):
        for ch in el:
            ln = local(ch.tag)
            if ln == "Fallback":
                continue
            if ln == "p":
                buf = []
                _para_runs(ch, buf, False)
                s = "".join(buf)
                if s.strip():
                    out.append(s)
            walk(ch)

    for ch in p:
        if local(ch.tag) in ("pPr",):
            continue
        walk(ch)
    return out


class _Styles:
    #------------------------------------------------------------------
    # 스타일 표 읽기
    #=> styles.xml 의 문단 스타일마다 (이름, basedOn, outlineLvl) 을 모아 둔다.
    #
    # -in: root = styles.xml 루트(없으면 None)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, root):
        self.info = {}          # styleId → (name, basedOn, outlineLvl 또는 None)
        self.default = None     # 기본 문단 스타일 id
        if root is None:
            return
        for st in root:
            if local(st.tag) != "style" or attr(st, "type") not in ("paragraph", None):
                continue
            sid = attr(st, "styleId")
            name = attr(child(st, "name"), "val") or ""
            based = attr(child(st, "basedOn"), "val")
            lvl = attr(child(child(st, "pPr"), "outlineLvl"), "val")
            self.info[sid] = (name.strip(), based, int(lvl) if lvl and lvl.isdigit() else None)
            if attr(st, "default") in ("1", "true"):
                self.default = sid

    #------------------------------------------------------------------
    # 스타일의 성격 판정 — 제목 수준 / 목차 / 캡션
    #=> basedOn 사슬을 따라 올라가며 처음 나오는 단서를 쓴다(최대 10단계, 순환 방어).
    #
    # -in: sid = 문단의 pStyle 값(없으면 기본 스타일)
    #
    # -out: (kind, level) — kind ∈ {"heading", "toc", "caption", None}
    # -out: error = 없음
    #------------------------------------------------------------------
    def kind(self, sid):
        sid = sid or self.default
        for _ in range(10):
            if sid not in self.info:
                return None, 0
            name, based, lvl = self.info[sid]
            if _TOC_NAME.match(name):
                return "toc", 0
            if _CAPTION_NAME.match(name):
                return "caption", 0
            m = _HEADING_NAME.match(name)
            if m:
                return "heading", max(1, int(m.group(1)))
            if _TITLE_NAME.match(name):
                return "heading", 1
            # outlineLvl 9 는 '본문 수준'이라는 뜻이다
            if lvl is not None and lvl < 9:
                return "heading", lvl + 1
            sid = based
        return None, 0


#------------------------------------------------------------------
# DOCX 추출 (진입점)
#
# -in: path  = .docx 경로
# -in: title = 문서 제목
#
# -out: Document (블록 경로, 후처리 전)
# -out: error = ZIP 아님·본문 없음·XML 손상이면 ValueError (상위가 legacy 로 폴백)
#------------------------------------------------------------------
def extract(path, title=""):
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            body_root = parse(z, "word/document.xml")
            if body_root is None:
                raise ValueError("docx 본문(word/document.xml) 없음·손상")
            styles = _Styles(parse(z, "word/styles.xml"))
            extras = sorted(n for n in names if n.endswith(".xml")
                            and (n.startswith("word/header") or n.startswith("word/footer")))
            extra_roots = [(n, parse(z, n)) for n in extras]
            foot_root = parse(z, "word/footnotes.xml") if "word/footnotes.xml" in names else None
    except zipfile.BadZipFile as e:
        raise ValueError("docx 열기 실패: {}".format(type(e).__name__))

    doc = B.Document(format="docx", title=title)
    body = child(body_root, "body")
    if body is None:
        raise ValueError("docx 본문(<w:body>) 없음")
    _walk(body, styles, doc.blocks, in_toc=False)

    # 머리말·꼬리말 — 표시만 하고 청크에서는 뺀다(진단 JSON 에는 남는다)
    for n, root in extra_roots:
        if root is None:
            continue
        kind = B.HEADER if "/header" in n else B.FOOTER
        for p in root.iter():
            if local(p.tag) == "p":
                buf = []
                _para_runs(p, buf, False)
                s = "".join(buf)
                if s.strip():
                    doc.blocks.append(B.Block(kind, s))

    # 각주 — 본문과 섞이지 않게 문서 끝의 '각주' 절로 모은다
    notes = []
    if foot_root is not None:
        for fn in foot_root:
            # 구분선·이어짐 표시 같은 장식용 각주(type 속성 있음)는 글자가 없다
            if local(fn.tag) != "footnote" or attr(fn, "type"):
                continue
            buf = []
            for p in fn.iter():
                if local(p.tag) == "p":
                    _para_runs(p, buf, False)
                    buf.append("\n")
            s = "".join(buf).strip()
            if s:
                notes.append(s)
    if notes:
        doc.blocks.append(B.Block(B.HEADING, "각주", level=1, synthetic=True))
        doc.blocks.extend(B.Block(B.NOTE, s) for s in notes)
    return doc


#------------------------------------------------------------------
# 본문 요소를 문서 순서로 돌기
#=> <w:body> 의 자식은 문단(p)·표(tbl)·내용 컨트롤(sdt)·사용자 XML 래퍼 등이다.
#    1) p   → 문단/제목/목록/목차 블록 (+ 안쪽 텍스트상자 문단)
#    2) tbl → 표 블록
#    3) sdt → 내용(sdtContent)으로 내려간다. 목차 문서부품이면 안의 문단을 모두 toc 로
#    4) 그 밖의 래퍼(customXml·smartTag 등) → 안으로 내려간다
#
# -in: el     = 훑을 부모 요소
# -in: styles = _Styles
# -in: out    = 블록을 쌓을 리스트(제자리)
# -in: in_toc = 목차 문서부품 안인가
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _walk(el, styles, out, in_toc):
    for ch in el:
        ln = local(ch.tag)
        if ln == "p":
            _paragraph(ch, styles, out, in_toc)
        elif ln == "tbl":
            rows, nh = _table(ch)
            if rows:
                out.append(B.Block(B.TABLE, rows=rows, header_rows=nh))
        elif ln == "sdt":
            gallery = ""
            pr = child(ch, "sdtPr")
            if pr is not None:
                for e in pr.iter():
                    if local(e.tag) == "docPartGallery":
                        gallery = attr(e, "val") or ""
            _walk(child(ch, "sdtContent") if child(ch, "sdtContent") is not None else ch,
                  styles, out, in_toc or "table of contents" in gallery.lower())
        elif ln in ("sectPr", "bookmarkStart", "bookmarkEnd", "proofErr"):
            continue
        else:
            _walk(ch, styles, out, in_toc)


#------------------------------------------------------------------
# 문단 하나 → 블록
#=> 글자를 모은 뒤 스타일·직접 지정·번호 매기기로 종류를 정한다.
#   우선순위: 목차 > 제목 > 캡션 > 목록 > 본문.
#
# -in: p      = <w:p>
# -in: styles = _Styles
# -in: out    = 블록 리스트(제자리)
# -in: in_toc = 목차 문서부품 안인가
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _paragraph(p, styles, out, in_toc):
    buf = []
    _para_runs(p, buf, False)
    text = "".join(buf)
    ppr = child(p, "pPr")
    kind, level = styles.kind(attr(child(ppr, "pStyle"), "val"))
    # 문단에 직접 지정한 개요 수준이 스타일보다 우선한다
    direct = attr(child(ppr, "outlineLvl"), "val")
    if direct and direct.isdigit() and int(direct) < 9:
        kind, level = "heading", int(direct) + 1
    numpr = child(ppr, "numPr")

    if text.strip():
        if in_toc or kind == "toc":
            out.append(B.Block(B.TOC, text))
        elif kind == "heading":
            out.append(B.Block(B.HEADING, text, level=level))
        elif kind == "caption":
            out.append(B.Block(B.CAPTION, text))
        elif numpr is not None:
            ilvl = attr(child(numpr, "ilvl"), "val")
            out.append(B.Block(B.LIST_ITEM, text,
                               level=(int(ilvl) + 1) if ilvl and ilvl.isdigit() else 1))
        else:
            out.append(B.Block(B.PARAGRAPH, text))

    # 텍스트상자 속 문단은 바깥 문단 뒤에 제 블록으로 낸다
    for s in _inner_paragraphs(p):
        out.append(B.Block(B.TOC if in_toc else B.PARAGRAPH, s))


#------------------------------------------------------------------
# 표 하나 → 행렬
#=> 행(<w:tr>)마다 칸(<w:tc>)을 열 위치에 놓는다.
#    · gridBefore — 행 앞에 비워 둔 열 수만큼 건너뛴다
#    · gridSpan   — 가로 병합(첫 칸에만 글자)
#    · vMerge     — 세로 병합. "restart" 가 시작, 값 없는 vMerge 는 위 칸의 이어짐 →
#                   위 칸 글자를 되풀이한다(행마다 이름표가 있어야 행 단독으로 해석된다)
#    · 칸 안의 문단은 줄바꿈으로, 칸 안에 든 표는 렌더링한 줄로 이어 붙인다
#    · 첫 행부터 이어지는 tblHeader 행 수 = 머리글 행 수
#
# -in: tbl = <w:tbl>
#
# -out: (rows, header_rows) — 내용 없으면 ([], 0)
# -out: error = 없음
#------------------------------------------------------------------
def _table(tbl):
    trs = [tr for tr in tbl if local(tr.tag) == "tr"]
    tg = child(tbl, "tblGrid")
    # Element 의 참거짓은 '자식 유무'라 or 로 기본값을 주면 안 된다 — None 으로 직접 가른다
    grid_cols = [g for g in tg if local(g.tag) == "gridCol"] if tg is not None else []
    raw = []           # 행마다 [(열, 글자, span, vmerge), …]
    width = len(grid_cols)
    header = 0
    counting_header = True
    for tr in trs:
        trpr = child(tr, "trPr")
        is_head = trpr is not None and child(trpr, "tblHeader") is not None \
            and attr(child(trpr, "tblHeader"), "val") not in ("0", "false")
        if counting_header and is_head:
            header += 1
        else:
            counting_header = False
        col = 0
        before = attr(child(trpr, "gridBefore"), "val") if trpr is not None else None
        if before and before.isdigit():
            col = int(before)
        cells = []
        for tc in tr:
            if local(tc.tag) != "tc":
                continue
            tcpr = child(tc, "tcPr")
            span = attr(child(tcpr, "gridSpan"), "val")
            span = int(span) if span and span.isdigit() else 1
            vm = child(tcpr, "vMerge")
            vmerge = None if vm is None else (attr(vm, "val") or "continue")
            cells.append((col, _cell_text(tc), span, vmerge))
            col += span
        width = max(width, col)
        raw.append(cells)

    grid = tables.new_grid(len(raw), width)
    for r, cells in enumerate(raw):
        for col, text, span, vmerge in cells:
            if vmerge == "continue" and r > 0 and col < width:
                # 세로 병합 이어짐 — 위 칸 글자를 되풀이
                text = grid[r - 1][col] or ""
            tables.place(grid, r, col, text, 1, span)
    rows = tables.finish(grid)
    return rows, header


#------------------------------------------------------------------
# 칸 하나의 글자
#
# -in: tc = <w:tc>
#
# -out: str = 칸 안 문단들을 줄바꿈으로 이은 글자
# -out: error = 없음
#------------------------------------------------------------------
def _cell_text(tc):
    lines = []
    for ch in tc:
        ln = local(ch.tag)
        if ln == "p":
            buf = []
            _para_runs(ch, buf, False)
            lines.append("".join(buf))
            lines.extend(_inner_paragraphs(ch))
        elif ln == "tbl":
            # 칸 안의 표 — 렌더링한 줄로 이어 붙여 글자를 잃지 않는다
            rows, _ = _table(ch)
            lines.extend(" ".join(c for c in row if c) for row in rows)
        elif ln == "sdt":
            content = child(ch, "sdtContent")
            for p in (content if content is not None else []):
                if local(p.tag) == "p":
                    buf = []
                    _para_runs(p, buf, False)
                    lines.append("".join(buf))
    return "\n".join(s for s in lines if s.strip())
