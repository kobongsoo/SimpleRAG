#------------------------------------------------------------------
# PDF 테두리 표 검출 (선 격자 → 셀)
#=> PDF 에는 '표'라는 표시가 없다. 대신 표 테두리는 가로·세로 **선(경로 객체)** 으로 그려져 있다.
#   그 선을 pdfium 으로 읽어 격자를 만들고, 글자를 위치에 따라 셀에 넣는다
#   (pdfplumber · OpenDataLoader 의 테두리 표 검출과 같은 방식, 새 의존성 없음).
#    1) 쪽의 경로 객체 중 얇고 긴 것은 선, 사각형은 네 변을 선으로 모은다
#    2) 서로 닿는 선끼리 묶어 표 후보로 만든다(가로 2개·세로 2개 이상)
#    3) 선 좌표를 모아 격자(행·열 경계)를 만들고, 경계선이 없는 칸끼리는 병합 셀로 묶는다
#    4) 글자를 셀에 넣는다 — 가로는 글자 상자 중심, 세로는 기준선(쉼표처럼 낮은 글자도 같은 줄)
#    5) 장식 상자·글 상자·목차 상자처럼 표가 아닌 것을 거른다
#   테두리 없는 표는 찾지 않는다 — 기존처럼 줄 단위 본문으로 남는다.
#   실측: bench/compare_pdf_parsers.py · report/PDF파서_비교_OpenDataLoader_20261111.html
#------------------------------------------------------------------

import re

try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as _raw
except ImportError:          # pragma: no cover — requirements 에 있다
    pdfium = None
    _raw = None

TOL = 2.0              # 같은 선·같은 좌표로 보는 차이(pt)
THIN = 2.5             # 이보다 얇으면 선으로 본다(pt)
MIN_SEG = 3.0          # 이보다 짧은 선은 무시(글자 장식·밑줄 조각)
MAX_PATHS = 5000       # 경로가 이보다 많으면 그림·도형 쪽으로 보고 건너뛴다(속도 방어)
MIN_REAL_ROWS = 2      # 서로 다른 글이 2칸 이상 있는 행이 이만큼은 있어야 표
MIN_REAL_SHARE = 0.5   # …그리고 그런 행이 전체 행의 이 비율 이상
MAX_EMPTY_SHARE = 0.5  # 빈 칸이 이 비율을 넘으면 장식 상자로 본다
MIN_TABLE_CHARS = 10   # 표 전체 글자(서로 다른 셀 기준)가 이보다 적으면 표지 장식으로 본다
MAX_ONE_CELL = 0.7     # 한 셀 글자가 표 전체 글자의 이 비율을 넘으면 글 상자로 본다
MAX_MEDIAN_CELL = 200  # 셀 글자 수 중앙값이 이보다 길면 2단 편집 상자로 본다
MAX_BREAK_SHARE = 0.3  # 셀 안 줄바꿈이 글자 수의 이 비율을 넘으면 세로 글씨로 본다
MAX_EXPAND = 2.5       # 병합 셀을 펼친 글이 원래 글의 이 배를 넘으면 레이아웃 격자로 본다
_LEADER = re.compile(r"(?:\.{3,}|·{3,}|…{2,}|-{4,})\s*\d{0,4}")


#------------------------------------------------------------------
# 쪽의 선 모으기
#=> 경로 객체의 외곽 상자로 선을 판정한다. 셀 테두리를 사각형으로 그리는 문서가 있어
#   사각형은 네 변을 모두 선으로 넣는다(배경 칠 사각형도 결국 셀 경계와 겹친다).
#
# -in: page = PdfPage
#
# -out: (H, V) — H: [(y, x0, x1)] 가로선, V: [(x, y0, y1)] 세로선 (PDF 좌표, y 는 위로 증가)
# -out: error = 없음 (경로가 MAX_PATHS 를 넘으면 빈 목록)
#------------------------------------------------------------------
def _segments(page):
    H, V = [], []
    n = 0
    for o in page.get_objects(filter=[_raw.FPDF_PAGEOBJ_PATH], max_depth=5):
        n += 1
        if n > MAX_PATHS:
            return [], []
        l, b, r, t = o.get_bounds()
        w, h = r - l, t - b
        if h < THIN and w > MIN_SEG:
            H.append(((b + t) / 2, l, r))
        elif w < THIN and h > MIN_SEG:
            V.append(((l + r) / 2, b, t))
        elif w >= THIN and h >= THIN:
            H += [(b, l, r), (t, l, r)]
            V += [(l, b, t), (r, b, t)]
    return H, V


#------------------------------------------------------------------
# 가까운 좌표끼리 하나로
#=> 선 두께·그리기 오차로 같은 경계가 1~2pt 씩 어긋나 있다. TOL 안의 값은 평균 하나로 친다.
#
# -in: vals = 좌표 목록
#
# -out: list[float] — 오름차순 대표 좌표
# -out: error = 없음
#------------------------------------------------------------------
def _cluster(vals):
    groups = []
    for v in sorted(vals):
        if groups and v - groups[-1][-1] <= TOL:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


#------------------------------------------------------------------
# 서로 닿는 선끼리 표 후보로 묶기
#=> 가로선과 세로선이 (오차 안에서) 교차하거나 맞닿으면 같은 표다. 합집합-찾기로 묶는다.
#
# -in: H = 가로선 목록
# -in: V = 세로선 목록
#
# -out: list[(h 목록, v 목록)] — 가로·세로 각 2개 이상인 묶음만
# -out: error = 없음
#------------------------------------------------------------------
def _components(H, V):
    n = len(H) + len(V)
    par = list(range(n))

    # 대표 찾기(경로 압축)
    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a

    for i, (y, x0, x1) in enumerate(H):
        for j, (x, y0, y1) in enumerate(V):
            if x0 - TOL <= x <= x1 + TOL and y0 - TOL <= y <= y1 + TOL:
                par[find(i)] = find(len(H) + j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idx in groups.values():
        h = [H[i] for i in idx if i < len(H)]
        v = [V[i - len(H)] for i in idx if i >= len(H)]
        if len(h) >= 2 and len(v) >= 2:
            out.append((h, v))
    return out


#------------------------------------------------------------------
# 경계선이 있나
#=> 칸 사이 경계 위치에 그 칸 높이(너비)의 가운데를 지나는 선이 있으면 경계가 있다.
#   없으면 두 칸은 병합 셀이다.
#
# -in: segs = 같은 방향 선 목록 [(좌표, 시작, 끝)]
# -in: at   = 경계 좌표
# -in: a, b = 칸의 시작·끝(반대 축)
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _has_line(segs, at, a, b):
    m = (a + b) / 2
    return any(abs(s - at) <= TOL and s0 - TOL <= m <= s1 + TOL for s, s0, s1 in segs)


#------------------------------------------------------------------
# 표 후보 하나 → 행렬
#=> 격자를 만들고 병합 셀을 묶은 뒤 글자를 넣는다. 병합 셀은 펼쳐 같은 글자로 채운다
#   (프로젝트 표 관례 — blocks.Block.rows 참고).
#    · 위·아래 끝의 전체 폭 한 칸 행(쪽 배경 상자에 딸려 온 제목·문단)은 떼어 본문에 남긴다
#    · 빈 행·열은 지우고, 병합 셀로 똑같이 되풀이되는 이웃 열은 하나로 합친다
#    · 셀 안 줄바꿈이 글자 수의 MAX_BREAK_SHARE 를 넘으면 세로 글씨 장식으로 보고 버린다
#    · 병합 셀을 펼친 글이 원래 글의 MAX_EXPAND 배를 넘으면 쪽 레이아웃 격자로 보고 버린다
#
# -in: h     = 이 표의 가로선
# -in: v     = 이 표의 세로선
# -in: chars = [(글자, x 중심, 기준선 y, 글자 번호)] 쪽 전체
#
# -out: (rows, bbox, used) — rows 행렬, bbox (x0, y0, x1, y1), used = 셀에 들어간 글자 번호 집합
#       표로 볼 수 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def _build(h, v, chars):
    ys = _cluster([s[0] for s in h])[::-1]      # 위 → 아래
    xs = _cluster([s[0] for s in v])            # 왼쪽 → 오른쪽
    nr, nc = len(ys) - 1, len(xs) - 1
    if nr < 1 or nc < 2:
        return None
    par = {(r, c): (r, c) for r in range(nr) for c in range(nc)}

    # 병합 셀 대표 찾기
    def find(a):
        while par[a] != a:
            a = par[a]
        return a

    for r in range(nr):
        for c in range(nc):
            # 오른쪽 경계선이 없으면 옆 칸과, 아래 경계선이 없으면 아래 칸과 같은 셀
            if c + 1 < nc and not _has_line(v, xs[c + 1], ys[r + 1], ys[r]):
                par[find((r, c + 1))] = find((r, c))
            if r + 1 < nr and not _has_line(h, ys[r + 1], xs[c], xs[c + 1]):
                par[find((r + 1, c))] = find((r, c))

    text, last_y, idxs = {}, {}, {}
    breaks = 0          # 셀 안 줄바꿈 수 — 세로 글씨 판정용
    for ch, cx, cy, idx in chars:
        if not (xs[0] <= cx <= xs[-1] and ys[-1] <= cy <= ys[0]):
            continue
        c = max(i for i in range(nc) if xs[i] <= cx)
        r = max(i for i in range(nr) if ys[i] >= cy)
        key = find((min(r, nr - 1), min(c, nc - 1)))
        s = text.get(key, "")
        # 셀 안에서 줄이 바뀌면 공백 하나 — 줄바꿈된 낱말 사이가 붙지 않게
        if key in last_y and abs(last_y[key] - cy) > 3:
            breaks += 1
            if s and not s.endswith(" "):
                s += " "
        # U+FFFE 등 pdfium 이 못 읽은 글자는 버린다
        text[key] = s + (ch if ch not in "￾￿" else "")
        last_y[key] = cy
        idxs.setdefault(key, set()).add(idx)
    if not idxs:
        return None
    # 글자마다 줄이 바뀌면 세로 글씨(옆 띠 장식) — 표가 아니다
    if breaks > sum(len(v) for v in idxs.values()) * MAX_BREAK_SHARE:
        return None

    keys =[[find((r, c)) for c in range(nc)] for r in range(nr)]
    rows = [[" ".join(text.get(k, "").split()) for k in krow] for krow in keys]
    # 위·아래 끝의 '전체 폭 한 칸' 행은 표 밖 글(쪽 배경 상자에 같이 묶인 제목·문단)이다 — 떼어 본문으로 돌린다
    while keys and len(set(keys[0])) == 1:
        keys, rows = keys[1:], rows[1:]
    while keys and len(set(keys[-1])) == 1:
        keys, rows = keys[:-1], rows[:-1]
    if not rows:
        return None
    used = set()
    for krow in keys:
        for k in set(krow):
            used |= idxs.get(k, set())
    # 빈 행·빈 열을 지우고, 병합 셀 때문에 똑같이 되풀이되는 이웃 열은 하나로 합친다
    pairs = [(row, krow) for row, krow in zip(rows, keys) if any(row)]
    cols = [c for c in range(nc) if any(row[c] for row, _ in pairs)]
    dedup = []
    for c in cols:
        if dedup and all(krow[c] == krow[dedup[-1]] for _, krow in pairs):
            continue
        dedup.append(c)
    rows = [[row[c] for c in dedup] for row, _ in pairs]
    if len(rows) < 2 or len(dedup) < 2 or not used:
        return None
    # 병합 셀을 펼친 글이 원래 글의 몇 배로 불어나면 쪽 레이아웃 격자다(OWASP 위험 요소 쪽:
    #   열 33개에 700자 셀이 수십 번 복제돼 한 쪽이 4만 자) — 표로 쓰지 않는다
    unique = sum(len(text.get(k, "")) for k in {krow[c] for _, krow in pairs for c in dedup})
    expanded = sum(len(c) for row in rows for c in row)
    if unique and expanded > unique * MAX_EXPAND:
        return None
    return rows, (xs[0], ys[-1], xs[-1], ys[0]), used


#------------------------------------------------------------------
# 표가 아닌 상자 거르기
#=> 선으로 둘러싸였다고 다 표가 아니다(상담사례집 실측: 표지 장식 · CONTENTS 상자 · 글 상자).
#    1) 서로 다른 글이 2칸 이상 있는 행이 MIN_REAL_ROWS 개 미만이거나 전체 행의
#       MIN_REAL_SHARE 미만 → 한 줄 상자·장식·그래프 범례
#    2) 빈 칸이 MAX_EMPTY_SHARE 를 넘음 → 장식 띠·세로 글씨 상자
#    3) 표 전체 글자가 MIN_TABLE_CHARS 미만 → 표지 장식
#    4) 셀 글자 수 중앙값이 MAX_MEDIAN_CELL 초과 → 2단 편집 상자(OWASP 소개글)
#    5) 한 셀(서로 다른 셀 기준) 글자가 전체의 MAX_ONE_CELL 를 넘음 → 글 상자
#    6) 점선 리더(……… 12)가 2칸 이상 → 목차 상자
#   (세로 글씨 상자는 _build 가 셀 안 줄바꿈 비율로 먼저 거른다)
#
# -in: rows = 행렬
#
# -out: bool — True 면 표로 쓴다
# -out: error = 없음
#------------------------------------------------------------------
def _looks_like_table(rows):
    real = sum(1 for row in rows if len({c for c in row if c}) >= 2)
    if real < MIN_REAL_ROWS or real < len(rows) * MIN_REAL_SHARE:
        return False
    cells = [c for row in rows for c in row]
    # 빈 칸이 대부분이면 장식 띠·세로 글씨 상자(상담사례집 옆 띠)
    if sum(1 for c in cells if not c) > len(cells) * MAX_EMPTY_SHARE:
        return False
    distinct = {}
    for row in rows:
        for c in row:
            if c:
                distinct[c] = len(c)
    total = sum(distinct.values())
    if total < MIN_TABLE_CHARS:
        return False
    lens = sorted(distinct.values())
    # 셀 대부분이 긴 문단이면 2단 편집 상자
    if lens[len(lens) // 2] > MAX_MEDIAN_CELL:
        return False
    if max(distinct.values()) > total * MAX_ONE_CELL:
        return False
    if sum(1 for c in distinct if _LEADER.search(c)) >= 2:
        return False
    return True


#------------------------------------------------------------------
# 쪽 글자 위치 읽기
#=> pdfium 글자 번호와 get_text_range 글의 위치를 맞춰 가며 글자마다 좌표를 읽는다.
#   pdfium 은 UTF-16 단위로 세서 기본 다국어 평면 밖 글자(글머리표 '󰏚' 등)는 두 번호(상·하위
#   서로게이트)를 차지하지만 파이썬 글에서는 한 글자다 — 하위 번호는 건너뛰고 위치를 하나만 센다.
#   끝까지 맞춰 봐서 글 길이와 다르면 위치를 믿을 수 없으므로 None.
#
# -in: tp  = PdfTextPage
# -in: txt = tp.get_text_range() 결과
#
# -out: [(글자, x 중심, 기준선 y, 글 위치)] — 줄바꿈 제외. 맞출 수 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def _page_chars(tp, txt):
    x = _raw.ctypes.c_double()
    y = _raw.ctypes.c_double()
    out, pos = [], 0
    n = tp.count_chars()
    i = 0
    while i < n:
        if pos >= len(txt):
            return None
        u = _raw.FPDFText_GetUnicode(tp.raw, i)
        ch = txt[pos]
        if ch not in "\r\n":
            l, b, r, t = tp.get_charbox(i)
            _raw.FPDFText_GetCharOrigin(tp.raw, i, x, y)
            out.append((ch, (l + r) / 2, y.value, pos))
        # 상위 서로게이트면 다음 번호(하위)는 같은 글자 — 건너뛴다
        i += 2 if 0xD800 <= u < 0xDC00 else 1
        pos += 1
    return out if pos == len(txt) else None


#------------------------------------------------------------------
# 쪽 하나의 표 찾기 (진입점)
#=> 선이 없는 쪽은 선만 세고 바로 끝난다(대부분의 본문 쪽). 선이 있으면 글자 위치를
#   읽어 표를 만든다. 글자 번호(used)는 get_text_range 글 안의 위치다.
#
# -in: page = PdfPage
# -in: tp   = 그 쪽의 PdfTextPage
# -in: txt  = tp.get_text_range() 결과
#
# -out: list[(rows, bbox, used)] — 위에서 아래 순서. used = 표에 들어간 글 위치 집합
# -out: error = 없음 (pdfium 호출 실패·글자 위치 불일치 시 빈 목록 — 기존처럼 본문으로만 남는다)
#------------------------------------------------------------------
def find_tables(page, tp, txt):
    if pdfium is None or not txt:
        return []
    try:
        H, V = _segments(page)
        if len(H) < 2 or len(V) < 2:
            return []
        comps = _components(H, V)
        if not comps:
            return []
        chars = _page_chars(tp, txt)
        if chars is None:
            return []
        out = []
        for h, v in comps:
            got = _build(h, v, chars)
            if got and _looks_like_table(got[0]):
                out.append(got)
        # 읽기 순서 — 위(y 큰 쪽)부터
        out.sort(key=lambda t: -t[1][3])
        return out
    except Exception:           # noqa: BLE001 — 표 검출 실패는 본문 추출을 막지 않는다
        return []
