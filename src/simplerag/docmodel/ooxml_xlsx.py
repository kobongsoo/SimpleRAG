#------------------------------------------------------------------
# XLSX → 블록 (재설계서 §7)
#=> 엑셀은 **시트가 곧 절, 시트 내용이 곧 표**다.
#    · 시트 순서·이름 — workbook.xml 의 시트 목록(rels 로 실제 파일 연결)
#    · 셀 위치 — 셀 주소(r="B3")로 놓는다. 빈 칸을 건너뛴 채 이어 붙이면 열이 밀린다
#    · 병합 — <mergeCell ref="A1:C2"> 를 펼친다(세로는 되풀이, 가로는 첫 칸만)
#    · 옆으로 붙은 표 — 모든 행이 빈 열을 경계로 시트를 여러 표로 나눈다(_sheet_tables)
#    · 제목 줄 — 표 위쪽의 '한 칸만 찬 행'(시트 제목·단위 표기)은 문단으로 떼어 낸다.
#                그래야 그 아래 진짜 머리글 행이 머리글로 잡힌다
#    · 날짜 — 숫자로 저장된 날짜를 서식에 따라 YYYY-MM-DD 로(xlsdate)
#    · 셀 메모 — 문서 끝 '셀 메모' 절(note)로 모은다
#   이식 출처: MpowerClassify office_ooxml.py XlsxExtractor(공유문자열·날짜 서식·메모 읽기)
#------------------------------------------------------------------

import re
import zipfile

from . import blocks as B
from . import tables, xlsdate
from .ooxml_common import attr, cell_ref, child, descendants, local, num_key, parse, rels

_COMMENTS = re.compile(r"xl/comments(\d*)\.xml$", re.I)
_TCOMMENTS = re.compile(r"xl/threadedComments/threadedComment(\d*)\.xml$", re.I)
_SHEET = re.compile(r"xl/worksheets/sheet(\d+)\.xml$", re.I)

# 비정상적으로 큰 시트 방어 — 이를 넘는 부분은 버리고 경고를 남긴다.
#   행은 엑셀 최대 행 수까지 받는다. 처음엔 2만으로 막았는데, 실측(D:\분류함 전수 점검)에서
#   말뭉치 xlsx 한 건이 18만 낱말 중 40% 를 잃었다 — 기존 추출은 행 상한 없이 다 읽는다.
MAX_ROWS = 1048576
MAX_COLS = 256
# 세로 병합을 펼칠 최대 행 수(서식용으로 수천 행을 묶은 병합 방어)
MAX_MERGE_ROWS = 500
# 빈 열로 나눈 구간이 이보다 좁으면 표가 아니라 여백·주석 칸으로 보고 이웃 표에 붙인다
MIN_TABLE_COLS = 2


#------------------------------------------------------------------
# XLSX 추출 (진입점)
#
# -in: path  = .xlsx 경로
# -in: title = 문서 제목
#
# -out: Document (블록 경로, 후처리 전). pages = 시트 수
# -out: error = ZIP 아님이면 ValueError (상위가 legacy 로 폴백)
#------------------------------------------------------------------
def extract(path, title=""):
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError("xlsx 열기 실패: {}".format(type(e).__name__))
    with z:
        names = set(z.namelist())
        shared = _shared_strings(z, names)
        styles = _date_styles(z, names)
        sheets = _sheet_order(z, names)
        if not sheets:
            raise ValueError("xlsx 시트 없음")
        doc = B.Document(format="xlsx", title=title, pages=len(sheets))
        for no, (name, part) in enumerate(sheets, 1):
            root = parse(z, part)
            if root is None:
                doc.warnings.append("시트 {} XML 손상".format(name))
                continue
            # 시트 이름은 경로 이름표로만 쓴다("Sheet1" 같은 이름이 본문을 채우지 않게)
            doc.blocks.append(B.Block(B.HEADING, name or "시트 {}".format(no), level=1,
                                      page=no, synthetic=True))
            # 한 시트에 표 여러 개가 빈 열을 사이에 두고 옆으로 붙어 있을 수 있다 — 표마다 따로 낸다
            for rows in _sheet_tables(root, shared, styles, doc.warnings, name):
                # 표 위쪽의 '한 칸만 찬 행'은 시트 제목·단위 표기다 — 문단으로 떼어 낸다
                while rows and sum(1 for c in rows[0] if c.strip()) <= 1:
                    line = next((c for c in rows.pop(0) if c.strip()), "")
                    if line:
                        doc.blocks.append(B.Block(B.PARAGRAPH, line, page=no))
                if rows:
                    doc.blocks.append(B.Block(B.TABLE, rows=rows, page=no))
        memos = _comments(z, names)
        if memos:
            doc.blocks.append(B.Block(B.HEADING, "셀 메모", level=1, synthetic=True))
            doc.blocks.extend(B.Block(B.NOTE, m) for m in memos)
    return doc


#------------------------------------------------------------------
# 시트 순서와 이름
#=> workbook.xml 의 <sheet name r:id> 를 rels 로 실제 시트 파일에 잇는다.
#   실패하면 파일 번호순·이름 없음으로 물러난다.
#
# -in: z     = 열린 ZipFile
# -in: names = zip 항목 이름 집합
#
# -out: [(시트 이름, 부품 경로), …]
# -out: error = 없음
#------------------------------------------------------------------
def _sheet_order(z, names):
    wb = parse(z, "xl/workbook.xml")
    if wb is not None:
        rmap = rels(z, "xl/workbook.xml")
        out = []
        for e in descendants(wb, "sheet"):
            rid = next((v for k, v in e.attrib.items() if local(k) == "id" and v in rmap), None)
            if rid and rmap[rid][1] in names:
                out.append((attr(e, "name") or "", rmap[rid][1]))
        if out:
            return out
    return [("", n) for n in sorted((n for n in names if _SHEET.search(n)),
                                    key=lambda n: num_key(n, _SHEET))]


#------------------------------------------------------------------
# 공유문자열표
#=> 엑셀은 반복 문자열을 xl/sharedStrings.xml 에 모아 두고 셀은 번호만 가진다.
#   <si> 하나가 서식 때문에 여러 <t> 로 쪼개져 있어도 하나로 잇는다.
#
# -in: z, names
#
# -out: list[str] (없으면 [])
# -out: error = 없음
#------------------------------------------------------------------
def _shared_strings(z, names):
    if "xl/sharedStrings.xml" not in names:
        return []
    root = parse(z, "xl/sharedStrings.xml")
    if root is None:
        return []
    out = []
    for si in root:
        if local(si.tag) != "si":
            continue
        # 윗주(rPh — 일본어 후리가나 등)는 본문이 아니다
        out.append("".join(e.text or "" for e in si.iter()
                           if local(e.tag) == "t" and not _in_phonetic(si, e)))
    return out


#------------------------------------------------------------------
# 윗주 안의 글자인가
#
# -in: si = <si> 요소
# -in: t  = 그 안의 <t> 요소
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _in_phonetic(si, t):
    for rph in si:
        if local(rph.tag) == "rPh" and any(e is t for e in rph.iter()):
            return True
    return False


#------------------------------------------------------------------
# 칸 서식 번호별 날짜 종류 (MpowerClassify _date_styles 이식)
#=> <cellXfs> 안의 <xf> 만 센다 — <cellStyleXfs> 까지 세면 번호가 밀려 멀쩡한 숫자가
#   날짜로 둔갑한다.
#
# -in: z, names
#
# -out: list[int] = KIND_* (서식표가 없으면 [])
# -out: error = 없음
#------------------------------------------------------------------
def _date_styles(z, names):
    if "xl/styles.xml" not in names:
        return []
    root = parse(z, "xl/styles.xml")
    if root is None:
        return []
    custom = {}
    for e in root.iter():
        if local(e.tag) != "numFmt":
            continue
        try:
            custom[int(e.get("numFmtId"))] = xlsdate.code_kind(e.get("formatCode"))
        except (TypeError, ValueError):
            continue
    out = []
    for grp in root.iter():
        if local(grp.tag) != "cellXfs":
            continue
        for xf in grp:
            if local(xf.tag) != "xf":
                continue
            try:
                fid = int(xf.get("numFmtId") or 0)
            except (TypeError, ValueError):
                fid = 0
            out.append(custom.get(fid, xlsdate.builtin_kind(fid)))
        break
    return out


#------------------------------------------------------------------
# 셀 하나의 표시 글자 (MpowerClassify _cell_value 이식)
#
# -in: c      = <c> 요소
# -in: shared = 공유문자열표
# -in: styles = 칸 서식별 날짜 종류
#
# -out: str (빈 셀이면 "")
# -out: error = 없음 (해석 실패 시 "")
#------------------------------------------------------------------
def _cell_value(c, shared, styles):
    t = c.get("t")
    v = child(c, "v")
    if t == "s":
        try:
            return shared[int(v.text)]
        except (AttributeError, ValueError, IndexError, TypeError):
            return ""
    if t == "inlineStr":
        return "".join(e.text or "" for e in c.iter() if local(e.tag) == "t")
    if v is None:
        return ""
    raw = v.text or ""
    # 숫자 칸이고 서식이 날짜면 사람이 보는 글자로 바꾼다
    if t in (None, "n") and styles:
        try:
            kind = styles[int(c.get("s"))]
        except (TypeError, ValueError, IndexError):
            kind = xlsdate.KIND_NONE
        shown = xlsdate.serial_to_string(raw, kind)
        if shown is not None:
            return shown
    return raw


#------------------------------------------------------------------
# 시트 → 행 목록
#=> 셀을 주소 자리에 놓고, 병합을 펼친 뒤, 실제로 쓰인 행만 차례로 모은다
#   (100,000번째 행에 셀 하나가 있어도 빈 행 10만 개를 만들지 않는다).
#
# -in: root     = 시트 XML 루트
# -in: shared   = 공유문자열표
# -in: styles   = 칸 서식별 날짜 종류
# -in: warnings = 경고를 쌓을 리스트(제자리)
# -in: name     = 시트 이름(경고문용)
#
# -in: keep_cols = True 면 빈 열을 지우지 않고 열 위치를 그대로 둔다(_sheet_tables 가 표를 나눌 때)
#
# -out: rows (내용 없으면 [])
# -out: error = 없음
#------------------------------------------------------------------
def _sheet_rows(root, shared, styles, warnings, name, keep_cols=False):
    cells = {}
    cut = False
    for row in root.iter():
        if local(row.tag) != "row":
            continue
        auto_c = 0
        try:
            auto_r = int(row.get("r")) - 1
        except (TypeError, ValueError):
            auto_r = None
        for c in row:
            if local(c.tag) != "c":
                continue
            pos = cell_ref(c.get("r"))
            if pos is None:
                # 주소 없는 셀(드묾) — 행 번호와 이 행 안의 차례로 놓는다
                if auto_r is None:
                    continue
                pos = (auto_r, auto_c)
            r, col = pos
            auto_c = col + 1
            if r >= MAX_ROWS or col >= MAX_COLS:
                cut = True
                continue
            val = _cell_value(c, shared, styles)
            if val.strip():
                cells[(r, col)] = val
    if cut:
        warnings.append("시트 {} — {}행·{}열을 넘는 부분 생략".format(name, MAX_ROWS, MAX_COLS))

    # 병합 펼치기 — 왼쪽 위 칸의 글자를 세로로 되풀이(가로는 첫 칸만)
    for mc in descendants(root, "mergeCell"):
        ref = (mc.get("ref") or "").split(":")
        if len(ref) != 2:
            continue
        a, b = cell_ref(ref[0]), cell_ref(ref[1])
        if a is None or b is None or a not in cells:
            continue
        text = cells[a]
        for r in range(a[0] + 1, min(b[0], a[0] + MAX_MERGE_ROWS) + 1):
            cells.setdefault((r, a[1]), text)
    if not cells:
        return []

    used_rows = sorted({r for r, _ in cells})
    width = max(col for _, col in cells) + 1
    grid = tables.new_grid(len(used_rows), width)
    for i, r in enumerate(used_rows):
        for col in range(width):
            grid[i][col] = cells.get((r, col), "")
    if keep_cols:
        # 표 나누기용 — 빈 열을 지우지 않고 열 위치를 그대로 돌려준다
        return [[(x or "").strip() for x in row] for row in grid]
    return tables.finish(grid)


#------------------------------------------------------------------
# 시트 → 표 목록 (옆으로 붙은 표 나누기)
#=> 한 시트에 표 여러 개가 **빈 열을 사이에 두고 좌우로** 놓인 경우가 있다(저작물분류표: 3~5열짜리
#   분류표 15개). 하나의 넓은 표로 읽으면 "| no. | 권리분류 | 대분류 | 대분류 | 중분류 | no. | …" 처럼
#   뒤섞인 머리글이 조각마다 다시 붙어 검색을 흐린다(골든셋 v2 에서 근거 2문항 손실).
#    1) _sheet_rows 와 같은 방법으로 셀·병합을 모은다
#    2) 사용된 모든 행에서 비어 있는 열을 경계로 열 구간을 나눈다
#    3) 2열 미만인 구간은 장식용 칸일 수 있어 왼쪽(없으면 오른쪽) 구간에 붙인다
#       — 한 표 안의 빈 여백 열 하나 때문에 표가 쪼개지는 것을 막는다
#    4) 구간마다 따로 행렬을 만든다(그 구간에서 빈 행은 tables.finish 가 걷어 낸다)
#   빈 열이 없는 시트(안전점검표·MCCS 등)는 지금과 똑같이 표 하나다.
#
# -in: root, shared, styles, warnings, name = _sheet_rows 와 같음
#
# -out: list[rows] — 표마다 행 목록(내용 없으면 [])
# -out: error = 없음
#------------------------------------------------------------------
def _sheet_tables(root, shared, styles, warnings, name):
    rows = _sheet_rows(root, shared, styles, warnings, name, keep_cols=True)
    if not rows:
        return []
    width = len(rows[0])
    empty = [j for j in range(width) if not any(r[j].strip() for r in rows)]
    # 빈 열을 경계로 열 구간을 만든다
    spans, start = [], None
    for j in range(width + 1):
        if j < width and j not in empty:
            if start is None:
                start = j
        elif start is not None:
            spans.append([start, j])
            start = None
    # 2열 미만 구간은 이웃 구간에 붙인다(경계의 빈 열까지 함께 — finish 가 다시 걷어 낸다)
    merged = []
    for s in spans:
        if s[1] - s[0] < MIN_TABLE_COLS and merged:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    if len(merged) > 1 and merged[0][1] - merged[0][0] < MIN_TABLE_COLS:
        merged[1][0] = merged[0][0]
        merged.pop(0)
    out = []
    for a, b in merged:
        part = tables.finish([row[a:b] for row in rows])
        if part:
            out.append(part)
    return out


#------------------------------------------------------------------
# 셀 메모 모으기 (MpowerClassify _read_comments 이식)
#=> 신청서·양식류는 '어떻게 적어라'가 전부 메모에 있는 일이 흔하다
#   (메모를 놓쳐 재현율 48.7% 였던 신청서 실측). 옛 방식과 스레드 댓글을 모두 읽는다.
#
# -in: z, names
#
# -out: list[str] = 메모마다 한 줄
# -out: error = 없음
#------------------------------------------------------------------
def _comments(z, names):
    picked = []
    for n in names:
        if _COMMENTS.search(n):
            picked.append((n, False))
        elif _TCOMMENTS.search(n):
            picked.append((n, True))
    picked.sort(key=lambda it: num_key(it[0], _TCOMMENTS if it[1] else _COMMENTS))
    out = []
    for n, threaded in picked:
        root = parse(z, n)
        if root is None:
            continue
        outer, inner = ("threadedComment", "text") if threaded else ("comment", "t")
        for c in root.iter():
            if local(c.tag) != outer:
                continue
            txt = "".join(e.text or "" for e in c.iter() if local(e.tag) == inner).strip()
            if txt:
                out.append(txt)
    return out
