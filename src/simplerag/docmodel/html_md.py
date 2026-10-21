#------------------------------------------------------------------
# HTML · Markdown · Jupyter 노트북 → 블록 (재설계서 §7)
#=> 세 포맷 모두 제목 수준이 글자 그대로 드러난다(h1~h6, '#').
#    · HTML — h1~h6 제목, p/li/div 문단, table(th = 머리글, colspan/rowspan),
#             script/style/nav/header/footer 요소는 버리거나 제외 블록으로
#    · MD   — '#' 제목, 파이프 표, ``` 코드 블록, 목록 항목, 빈 줄로 나뉜 문단
#    · IPYNB — 마크다운 셀은 MD 규칙으로, 코드 셀은 code 블록. 출력(outputs)은 계속 제외
#------------------------------------------------------------------

import json
import re
from html.parser import HTMLParser

from . import blocks as B
from . import tables


#------------------------------------------------------------------
# 텍스트 파일 읽기 (인코딩 추정)
#=> UTF-8(BOM 포함) → CP949(국내 레거시) 순으로 시도하고, 둘 다 안 되면 깨진 바이트를
#   대체문자로 바꿔 읽는다.
#
# -in: path = 파일 경로
#
# -out: str = 파일 내용
# -out: error = 파일을 열 수 없으면 OSError 전파
#------------------------------------------------------------------
def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ── Markdown ─────────────────────────────────────────
_MD_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_MD_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MD_LIST = re.compile(r"^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(.*)$")


#------------------------------------------------------------------
# Markdown 글 → 블록 목록
#=> 줄 단위로 읽으며 상태(코드 블록 안인가, 표가 이어지는가, 문단이 이어지는가)를 따라간다.
#    1) ``` 사이는 코드 블록 하나
#    2) '#' 줄은 제목(수준 = '#' 개수)
#    3) 파이프 줄이 이어지면 표 하나(구분선 '|---|' 앞 행 = 머리글)
#    4) '-'·'1.' 로 시작하면 목록 항목(깊이 = 들여쓰기 2칸당 1)
#    5) 나머지는 빈 줄까지 이어 문단 하나
#
# -in: text = Markdown 원문
#
# -out: list[Block]
# -out: error = 없음
#------------------------------------------------------------------
def md_blocks(text):
    out = []
    para, table, code = [], [], None

    # 이어지던 문단을 블록으로 낸다
    def flush_para():
        if para:
            s = "\n".join(para).strip()
            if s:
                out.append(B.Block(B.PARAGRAPH, s))
            para.clear()

    # 이어지던 표를 블록으로 낸다 — 구분선 줄이 있으면 그 앞 행이 머리글
    def flush_table():
        if not table:
            return
        rows, header = [], 0
        for ln in table:
            if _MD_TABLE_SEP.match(ln):
                header = len(rows)
                continue
            rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
        width = max(len(r) for r in rows) if rows else 0
        rows = [r + [""] * (width - len(r)) for r in rows]
        if rows:
            out.append(B.Block(B.TABLE, rows=rows, header_rows=header))
        table.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        if code is not None:
            # 코드 블록 안 — 닫는 울타리까지 그대로 모은다
            if _MD_FENCE.match(line):
                out.append(B.Block(B.CODE, "\n".join(code)))
                code = None
            else:
                code.append(line)
            continue
        if _MD_FENCE.match(line):
            flush_para()
            flush_table()
            code = []
            continue
        if _MD_TABLE.match(line) or (table and _MD_TABLE_SEP.match(line)):
            flush_para()
            table.append(line)
            continue
        flush_table()
        m = _MD_HEAD.match(line)
        if m:
            flush_para()
            out.append(B.Block(B.HEADING, m.group(2).strip() or m.group(1), level=len(m.group(1))))
            continue
        m = _MD_LIST.match(line)
        if m:
            flush_para()
            out.append(B.Block(B.LIST_ITEM, m.group(2), level=len(m.group(1)) // 2 + 1))
            continue
        if not line.strip():
            flush_para()
            continue
        para.append(line)
    if code is not None:
        # 닫히지 않은 코드 블록도 글자는 살린다
        out.append(B.Block(B.CODE, "\n".join(code)))
    flush_para()
    flush_table()
    return out


#------------------------------------------------------------------
# Markdown 파일 추출 (진입점)
#
# -in: path  = .md 경로
# -in: title = 문서 제목
#
# -out: Document
# -out: error = 읽기 실패 시 OSError 전파
#------------------------------------------------------------------
def extract_md(path, title=""):
    doc = B.Document(format="md", title=title)
    doc.blocks = md_blocks(read_text(path))
    return doc


#------------------------------------------------------------------
# Jupyter 노트북 추출 (진입점)
#=> 마크다운 셀은 Markdown 규칙으로 블록을 만들고, 코드 셀은 code 블록 하나로 낸다.
#   출력(outputs)·메타데이터는 노이즈라 기존 extract_ipynb 처럼 넣지 않는다.
#
# -in: path  = .ipynb 경로
# -in: title = 문서 제목
#
# -out: Document
# -out: error = JSON 이 아니면 ValueError 전파(상위가 legacy 로 폴백)
#------------------------------------------------------------------
def extract_ipynb(path, title=""):
    nb = json.loads(read_text(path))
    doc = B.Document(format="ipynb", title=title)
    for cell in nb.get("cells", []):
        src = cell.get("source", [])
        body = "".join(src) if isinstance(src, list) else str(src)
        if not body.strip():
            continue
        if cell.get("cell_type") == "markdown":
            doc.blocks.extend(md_blocks(body))
        else:
            doc.blocks.append(B.Block(B.CODE, body.rstrip()))
    return doc


# ── HTML ─────────────────────────────────────────────
_SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_EXCLUDE = {"nav": B.HEADER, "header": B.HEADER, "footer": B.FOOTER}
_BLOCK = {"p", "div", "section", "article", "main", "aside", "blockquote", "pre",
          "dd", "dt", "figcaption", "address", "form", "fieldset", "br", "hr"}
_HEAD = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _HtmlBlocks(HTMLParser):
    #------------------------------------------------------------------
    # HTML 파서 준비
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.buf = []
        self.kind = (B.PARAGRAPH, 0)
        self.skip = 0
        self.exclude = []          # 열린 nav/header/footer 스택
        self.tables = []           # 열린 표 스택 — [{rows, row, cell, header}]

    #------------------------------------------------------------------
    # 모아 둔 글자를 블록으로 내보내기
    #=> 표 칸 안이면 칸 글자로, 아니면 현재 종류(제목/목록/문단)의 블록으로 낸다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def _flush(self):
        s = " ".join("".join(self.buf).split())
        self.buf = []
        if not s:
            return
        if self.tables and self.tables[-1]["cell"] is not None:
            self.tables[-1]["cell"].append(s)
            return
        kind, level = self.kind
        if self.exclude:
            kind, level = self.exclude[-1], 0
        self.out.append(B.Block(kind, s, level=level))

    #------------------------------------------------------------------
    # 여는 태그
    #
    # -in: tag   = 태그 이름
    # -in: attrs = 속성 목록
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)
        if tag in _EXCLUDE:
            self._flush()
            self.exclude.append(_EXCLUDE[tag])
        elif tag in _HEAD:
            self._flush()
            self.kind = (B.HEADING, _HEAD[tag])
        elif tag == "li":
            self._flush()
            self.kind = (B.LIST_ITEM, 1)
        elif tag in _BLOCK:
            self._flush()
        elif tag == "table":
            self._flush()
            self.tables.append({"rows": [], "row": None, "cell": None, "header": 0})
        elif tag == "tr" and self.tables:
            # HTML 은 </td>·</tr> 를 생략해도 된다 — 열려 있던 칸·행을 먼저 닫는다
            if self.tables[-1]["cell"] is not None:
                self.handle_endtag("td")
            if self.tables[-1]["row"] is not None:
                self.handle_endtag("tr")
            self.tables[-1]["row"] = []
        elif tag in ("td", "th") and self.tables:
            if self.tables[-1]["cell"] is not None:
                self.handle_endtag("td")
            t = self.tables[-1]
            if t["row"] is None:
                t["row"] = []
            t["cell"] = []
            t["span"] = (_int(a.get("rowspan")), _int(a.get("colspan")), tag == "th")

    #------------------------------------------------------------------
    # 닫는 태그
    #
    # -in: tag = 태그 이름
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in _EXCLUDE and self.exclude:
            self._flush()
            self.exclude.pop()
        elif tag in _HEAD or tag == "li":
            self._flush()
            self.kind = (B.PARAGRAPH, 0)
        elif tag in _BLOCK:
            self._flush()
        elif tag in ("td", "th") and self.tables and self.tables[-1]["cell"] is not None:
            self._flush()
            t = self.tables[-1]
            rs, cs, is_th = t.get("span", (1, 1, False))
            t["row"].append(("\n".join(t["cell"]), rs, cs, is_th))
            t["cell"] = None
        elif tag == "tr" and self.tables and self.tables[-1]["row"] is not None:
            t = self.tables[-1]
            t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table" and self.tables:
            self._flush()
            if self.tables[-1]["cell"] is not None:
                self.handle_endtag("td")
            if self.tables[-1]["row"] is not None:
                self.handle_endtag("tr")
            t = self.tables.pop()
            rows, nh = _html_grid(t["rows"])
            if not rows:
                return
            if self.tables and self.tables[-1]["cell"] is not None:
                # 표 안의 표 — 바깥 칸 글자로 이어 붙인다
                self.tables[-1]["cell"].extend(" ".join(c for c in r if c) for r in rows)
            else:
                self.out.append(B.Block(B.TABLE, rows=rows, header_rows=nh))

    #------------------------------------------------------------------
    # 글자
    #
    # -in: data = 태그 사이 글자
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


#------------------------------------------------------------------
# HTML 표 행 → 격자
#=> rowspan 으로 아래 행에 이미 차지된 칸을 건너뛰며 놓는다.
#   첫 행부터 모든 칸이 <th> 인 행 수 = 머리글 행 수.
#
# -in: raw_rows = [[(글자, rowspan, colspan, th여부), …], …]
#
# -out: (rows, header_rows)
# -out: error = 없음
#------------------------------------------------------------------
def _html_grid(raw_rows):
    taken = {}
    placed = []
    width = 0
    for r, row in enumerate(raw_rows):
        c = 0
        for text, rs, cs, _ in row:
            while (r, c) in taken:
                c += 1
            placed.append((r, c, rs, cs, text))
            for dr in range(rs):
                for dc in range(cs):
                    taken[(r + dr, c + dc)] = True
            c += cs
        width = max(width, c)
    grid = tables.new_grid(len(raw_rows), width)
    for r, c, rs, cs, text in placed:
        tables.place(grid, r, c, text, rs, cs)
    nh = 0
    while nh < len(raw_rows) and raw_rows[nh] and all(th for *_, th in raw_rows[nh]):
        nh += 1
    return tables.finish(grid), nh


#------------------------------------------------------------------
# 정수 속성 읽기 (rowspan 등)
#
# -in: v = 속성 문자열
#
# -out: int (1 이상, 실패 시 1)
# -out: error = 없음
#------------------------------------------------------------------
def _int(v):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 1


#------------------------------------------------------------------
# HTML 파일 추출 (진입점)
#
# -in: path  = .html/.htm 경로
# -in: title = 문서 제목
#
# -out: Document
# -out: error = 읽기 실패 시 OSError 전파
#------------------------------------------------------------------
def extract_html(path, title=""):
    p = _HtmlBlocks()
    p.feed(read_text(path))
    p.close()
    p._flush()
    doc = B.Document(format="html", title=title)
    doc.blocks = p.out
    return doc
