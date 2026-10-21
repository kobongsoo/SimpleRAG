#------------------------------------------------------------------
# HWPX → 블록 (재설계서 §7)
#=> HWPX 는 ZIP 안의 OWPML(XML)이다. 문단(<hp:p>)마다 문단 모양 번호(paraPrIDRef)를
#   갖고, header.xml 의 문단 모양에 '개요 수준'이 들어 있다.
#    · 제목 — 문단 모양의 <hh:heading type="OUTLINE" level="n"> → 수준 n+1
#             (type NUMBER/BULLET 은 문단 번호·글머리표 → 목록 항목)
#    · 표   — <hp:tbl> → 행렬. 칸 주소(cellAddr)·병합(cellSpan) 반영, header="1" 칸 = 머리글
#    · 글상자·도형 속 글자 — 그 안의 문단 목록(subList)을 본문 문단으로
#    · 머리말·꼬리말 컨트롤 → 제외 블록, 각주·미주 → 문서 끝 '각주' 절
#   문단 순서·글자 이어 붙이기 규칙은 MpowerClassify hwpx_zip.py 를 따른다(서식 경계에서
#   <hp:t> 가 갈라져도 한 문단은 한 줄 — 낱말이 쪼개지지 않게).
#------------------------------------------------------------------

import re
import zipfile

from . import blocks as B
from . import tables
from .ooxml_common import attr, child, local, parse

_SECTION = re.compile(r"Contents/section(\d+)\.xml$", re.I)
# 글자 안에 끼는 특수 공백·기호 요소 — 공백 하나로 살린다
_SPACE_TAGS = ("fwSpace", "nbSpace", "hyphen")


#------------------------------------------------------------------
# HWPX 추출 (진입점)
#
# -in: path  = .hwpx 경로
# -in: title = 문서 제목
#
# -out: Document (블록 경로, 후처리 전)
# -out: error = ZIP 아님·본문 없음이면 ValueError (상위가 legacy 로 폴백)
#------------------------------------------------------------------
def extract(path, title=""):
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError("hwpx 열기 실패: {}".format(type(e).__name__))
    with z:
        names = sorted((n for n in z.namelist() if _SECTION.search(n)),
                       key=lambda n: int(_SECTION.search(n).group(1)))
        if not names:
            raise ValueError("hwpx 본문(Contents/section*.xml) 없음")
        kinds = _para_kinds(parse(z, "Contents/header.xml"))
        doc = B.Document(format="hwpx", title=title)
        notes = []
        for n in names:
            root = parse(z, n)
            if root is None:
                doc.warnings.append("{} XML 손상".format(n))
                continue
            w = _Walker(kinds, doc.blocks, notes)
            for p in root:
                if local(p.tag) == "p":
                    w.paragraph(p)
    if notes:
        doc.blocks.append(B.Block(B.HEADING, "각주", level=1, synthetic=True))
        doc.blocks.extend(B.Block(B.NOTE, s) for s in notes)
    return doc


#------------------------------------------------------------------
# 문단 모양 번호 → (종류, 수준)
#=> header.xml 의 <hh:paraPr id> 마다 <hh:heading type level> 을 읽는다.
#    · OUTLINE      → ("heading", level+1)  — 한글의 '개요' 문단
#    · NUMBER/BULLET → ("list", level+1)    — 문단 번호·글머리표
#
# -in: root = header.xml 루트(없으면 None)
#
# -out: dict = {paraPr id: (종류, 수준)}
# -out: error = 없음
#------------------------------------------------------------------
def _para_kinds(root):
    out = {}
    if root is None:
        return out
    for pp in root.iter():
        if local(pp.tag) != "paraPr":
            continue
        h = child(pp, "heading")
        if h is None:
            continue
        t = (attr(h, "type") or "").upper()
        try:
            lvl = int(attr(h, "level") or 0)
        except ValueError:
            lvl = 0
        if t == "OUTLINE":
            out[attr(pp, "id")] = ("heading", lvl + 1)
        elif t in ("NUMBER", "BULLET"):
            out[attr(pp, "id")] = ("list", lvl + 1)
    return out


class _Walker:
    #------------------------------------------------------------------
    # 문단 순회기 준비
    #
    # -in: kinds = _para_kinds 결과
    # -in: out   = 블록 리스트(제자리)
    # -in: notes = 각주 글자 리스트(제자리)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, kinds, out, notes):
        self.kinds = kinds
        self.out = out
        self.notes = notes

    #------------------------------------------------------------------
    # 문단 하나 처리 (핵심)
    #=> 문단 안의 런(<hp:run>)을 차례로 돌며 글자는 모으고, 표·글상자를 만나면 그때까지
    #   모은 글자를 먼저 블록으로 내보낸 뒤 표·글상자를 낸다 — 문서 순서가 지켜진다.
    #
    # -in: p    = <hp:p>
    # -in: kind_out = 이 문단 글자를 낼 블록 종류를 강제할 때(머리말 등). None 이면 문단 모양으로
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def paragraph(self, p, kind_out=None):
        buf = []
        kind, level = self.kinds.get(attr(p, "paraPrIDRef"), (None, 0))

        # 지금까지 모은 글자를 블록 하나로 내보낸다
        def flush():
            s = "".join(buf)
            buf.clear()
            if not s.strip():
                return
            if kind_out:
                self.out.append(B.Block(kind_out, s))
            elif kind == "heading":
                self.out.append(B.Block(B.HEADING, s, level=level))
            elif kind == "list":
                self.out.append(B.Block(B.LIST_ITEM, s, level=level))
            else:
                self.out.append(B.Block(B.PARAGRAPH, s))

        for run in p:
            if local(run.tag) != "run":
                continue
            for el in run:
                ln = local(el.tag)
                if ln == "t":
                    _t_text(el, buf)
                elif ln == "tbl":
                    flush()
                    rows, nh = self._table(el)
                    if rows:
                        self.out.append(B.Block(B.TABLE, rows=rows, header_rows=nh))
                elif ln == "ctrl":
                    self._ctrl(el)
                elif ln in ("secPr", "colPr"):
                    continue
                else:
                    # 글상자·도형·그림 캡션 — 안의 문단 목록(subList)을 본문으로 낸다
                    lists = _sublists(el)
                    if lists:
                        flush()
                        for sl in lists:
                            for q in sl:
                                if local(q.tag) == "p":
                                    self.paragraph(q, kind_out)
        flush()

    #------------------------------------------------------------------
    # 컨트롤 처리 — 머리말·꼬리말·각주
    #=> 머리말·꼬리말은 제외 블록으로(진단용으로만 남김), 각주·미주는 문서 끝 '각주' 절로.
    #   쪽번호·자동번호 같은 나머지 컨트롤은 글자가 없어 무시한다.
    #
    # -in: ctrl = <hp:ctrl>
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def _ctrl(self, ctrl):
        for el in ctrl:
            ln = local(el.tag)
            if ln in ("header", "footer"):
                kind = B.HEADER if ln == "header" else B.FOOTER
                for sl in _sublists(el):
                    for q in sl:
                        if local(q.tag) == "p":
                            self.paragraph(q, kind)
            elif ln in ("footNote", "endNote"):
                tmp = []
                w = _Walker(self.kinds, tmp, self.notes)
                for sl in _sublists(el):
                    for q in sl:
                        if local(q.tag) == "p":
                            w.paragraph(q)
                s = "\n".join(b.text for b in tmp if b.text)
                if s.strip():
                    self.notes.append(s)

    #------------------------------------------------------------------
    # 표 → 행렬
    #=> 칸마다 주소(cellAddr rowAddr/colAddr)와 병합(cellSpan rowSpan/colSpan)이 있어
    #   그대로 격자에 놓는다. 칸 안 문단은 줄바꿈으로, 칸 안 표는 렌더링 줄로 잇는다.
    #   첫 행부터 모든 칸이 header="1" 인 행 수 = 머리글 행 수.
    #
    # -in: tbl = <hp:tbl>
    #
    # -out: (rows, header_rows)
    # -out: error = 없음
    #------------------------------------------------------------------
    def _table(self, tbl):
        cells = []
        n_rows = _int(attr(tbl, "rowCnt"))
        n_cols = _int(attr(tbl, "colCnt"))
        head_rows = {}
        auto_r = 0
        for tr in tbl:
            if local(tr.tag) != "tr":
                continue
            auto_c = 0
            for tc in tr:
                if local(tc.tag) != "tc":
                    continue
                addr, span = child(tc, "cellAddr"), child(tc, "cellSpan")
                r = _int(attr(addr, "rowAddr"), auto_r)
                c = _int(attr(addr, "colAddr"), auto_c)
                rs = _int(attr(span, "rowSpan"), 1)
                cs = _int(attr(span, "colSpan"), 1)
                auto_c = c + cs
                head_rows.setdefault(r, True)
                if attr(tc, "header") not in ("1", "true"):
                    head_rows[r] = False
                cells.append((r, c, rs, cs, self._cell_text(tc)))
                n_rows = max(n_rows, r + rs)
                n_cols = max(n_cols, c + cs)
            auto_r += 1
        grid = tables.new_grid(n_rows, n_cols)
        for r, c, rs, cs, text in cells:
            tables.place(grid, r, c, text, rs, cs)
        nh = 0
        while head_rows.get(nh):
            nh += 1
        return tables.finish(grid), nh

    #------------------------------------------------------------------
    # 칸 하나의 글자
    #
    # -in: tc = <hp:tc>
    #
    # -out: str = 칸 안 블록 글자를 줄바꿈으로 이은 것(안쪽 표는 행마다 한 줄)
    # -out: error = 없음
    #------------------------------------------------------------------
    def _cell_text(self, tc):
        tmp = []
        w = _Walker(self.kinds, tmp, self.notes)
        for sl in _sublists(tc):
            for q in sl:
                if local(q.tag) == "p":
                    w.paragraph(q)
        lines = []
        for b in tmp:
            if b.type == B.TABLE:
                lines.extend(" ".join(c for c in row if c) for row in b.rows)
            elif b.text:
                lines.append(b.text)
        return "\n".join(lines)


#------------------------------------------------------------------
# <hp:t> 안의 글자
#=> <hp:t> 안에는 글자 사이에 <hp:tab/>·<hp:lineBreak/>·특수 공백 요소가 끼고,
#   그 뒤 글자는 요소의 tail 에 있다. 탭·줄바꿈을 살리지 않으면 두 줄이 한 낱말로 붙는다.
#
# -in: t   = <hp:t>
# -in: buf = 글자 조각 리스트(제자리)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _t_text(t, buf):
    buf.append(t.text or "")
    for sub in t:
        ln = local(sub.tag)
        if ln == "tab":
            buf.append("\t")
        elif ln == "lineBreak":
            buf.append("\n")
        elif ln in _SPACE_TAGS:
            buf.append(" ")
        buf.append(sub.tail or "")


#------------------------------------------------------------------
# 요소 아래 첫 단계의 문단 목록(subList)들
#=> 글상자·칸·머리말은 <hp:subList> 안에 문단을 담는다. 찾은 subList 안으로는 더
#   내려가지 않는다 — 그 안의 표·글상자는 문단 처리가 다시 맡는다(두 번 읽기 방지).
#
# -in: el = 시작 요소
#
# -out: list[요소] = subList 목록(문서 순서)
# -out: error = 없음
#------------------------------------------------------------------
def _sublists(el):
    out = []

    # subList 를 만나면 담고 그 아래로는 멈춘다
    def walk(e):
        for ch in e:
            if local(ch.tag) == "subList":
                out.append(ch)
            else:
                walk(ch)

    walk(el)
    return out


#------------------------------------------------------------------
# 정수 읽기
#
# -in: v       = 속성 문자열(없을 수 있음)
# -in: default = 실패 시 값
#
# -out: int
# -out: error = 없음
#------------------------------------------------------------------
def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
