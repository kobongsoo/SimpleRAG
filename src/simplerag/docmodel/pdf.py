#------------------------------------------------------------------
# PDF(텍스트층) → 블록 (재설계서 §9 P-1 — 쪽 경계 · 머리말/꼬리말 · 쪽번호 · 목차)
#=> PDF 에는 '제목'이라는 표시가 없다. 확실한 것부터 쌓는다.
#    · 쪽 경계 — 쪽마다 읽어 블록에 page 를 단다 → 청크 payload 의 page_from/to(출처 쪽 표시 재료)
#    · 머리말·꼬리말 — 쪽마다 맨 위·아래 2줄을 모아 숫자를 '#' 로 바꾼 뒤, 3쪽 이상에서
#                     되풀이되는 줄을 header/footer 로 표시(청크에서 제외, 처음 1개는 본문에 남김)
#    · 쪽번호 — 맨 위·아래 줄이 "12" · "- 12 -" · "3 / 30" · "Page 3" 꼴이면 page_number 로 표시
#    · 목차 — 점선 리더 줄은 공통 후처리(postprocess._mark_toc)가 toc 로 표시
#    · 제목(P-2) — 본문보다 확연히 큰 짧은 줄을 제목으로 추정(source=inferred, 크기 순위 = 수준).
#                 추정 제목이 3개 이상이면 후처리가 정규식 제목(조문·번호절)을 그 아래 수준으로 겹친다
#   한 줄을 블록 하나로 둔다 — 기존 청킹이 큰 절을 줄 단위로 나누던 것과 같은 단위다.
#   텍스트층이 사실상 없으면(스캔 PDF) ValueError → 기존 추출(사이냅 폴백 포함)로 보낸다.
#------------------------------------------------------------------

import re
import statistics
import time

from . import blocks as B

try:
    import pypdfium2 as pdfium
except ImportError:          # pragma: no cover — requirements 에 있다
    pdfium = None

MAX_PAGES = 3000             # 비정상적으로 큰 PDF 방어(넘으면 거기까지만, 경고)
MAX_SECONDS = 120            # 추출 시간 상한(넘으면 거기까지만, 경고)
EDGE_LINES = 2               # 머리말·꼬리말 후보로 보는 쪽 맨 위·아래 줄 수
MIN_REPEAT_PAGES = 3         # 같은 가장자리에 이만큼 다른 쪽에서 되풀이되면 머리말·꼬리말
MAX_HEADER_CHARS = 60        # 이보다 긴 줄은 머리말 후보로 보지 않는다(본문 문장)
MIN_HEADER_LETTERS = 4       # 숫자·기호를 빼고 남는 글자가 이보다 적으면 후보에서 뺀다
MIN_TEXT_CHARS = 20          # 문서 전체 글자(공백 제외)가 이보다 적으면 텍스트층 없음으로 본다

_PAGE_NO = re.compile(r"^\s*(?:[-–—]\s*)?\d{1,4}(?:\s*[-–—])?\s*$"
                      r"|^\s*\d{1,4}\s*/\s*\d{1,4}\s*$"
                      r"|^\s*(?:page|p\.?)\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.I)
_LINE_SPLIT = re.compile(r"\r\n|\r|\n")

# 글자 크기 제목 추정(P-2)
HEAD_RATIO = 1.12            # 본문 크기의 이 배 이상이면 제목 후보
HEAD_MAX_CHARS = 60          # 이보다 긴 줄은 제목으로 보지 않는다
MIN_INFERRED = 3             # 후보가 이보다 적으면 추정하지 않는다(정규식 폴백에 맡김)
HEAD_MAX_SHARE = 0.15        # 후보가 전체 줄의 이 비율을 넘으면 큰 글씨 문서로 보고 추정하지 않는다
MAX_LEVEL = 3                # 크기 순위로 매기는 제목 수준의 최대값
# 목차 줄 꼬리(점선 리더 + 쪽번호) — 큰 글씨 목차를 제목으로 잡지 않게
_TOC_TAIL = re.compile(r"(?:\.{3,}|·{3,}|…{2,}|-{4,})\s*\d{1,4}\s*$")


#------------------------------------------------------------------
# PDF 추출 (진입점)
#
# -in: path  = .pdf 경로
# -in: title = 문서 제목
#
# -out: Document (블록 경로, 후처리 전). pages = 읽은 쪽 수
# -out: error = 열기 실패·텍스트층 없음이면 ValueError (상위가 legacy 로 폴백)
#------------------------------------------------------------------
def extract(path, title=""):
    if pdfium is None:
        raise ValueError("pypdfium2 미설치")
    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as e:      # noqa: BLE001 — 손상·암호화 PDF 는 폴백
        raise ValueError("PDF 열기 실패: {}".format(type(e).__name__))
    doc = B.Document(format="pdf", title=title)
    pages, sizes = [], []
    t0 = time.monotonic()
    try:
        n = len(pdf)
        for i in range(n):
            if i >= MAX_PAGES or time.monotonic() - t0 > MAX_SECONDS:
                doc.warnings.append("PDF 부분 추출 {}/{}쪽 (상한)".format(i, n))
                break
            page = pdf[i]
            try:
                tp = page.get_textpage()
                txt = tp.get_text_range()
                lines = [ln.rstrip() for ln in _LINE_SPLIT.split(txt or "")]
                pages.append(lines)
                sizes.append(_line_sizes(tp, txt, len(lines)))
            finally:
                page.close()
    finally:
        pdf.close()
    doc.pages = len(pages)
    if sum(len(re.sub(r"\s+", "", "".join(p))) for p in pages) < MIN_TEXT_CHARS:
        raise ValueError("PDF 텍스트층 없음(스캔 PDF)")

    edge = _edge_kinds(pages)
    # 글자 크기 제목 추정은 설정으로 켤 때만(기본 끔 — config.PDF_INFER_HEADINGS 주석의 측정 결과)
    from .. import config
    heads = _infer_headings(pages, sizes, edge) if config.PDF_INFER_HEADINGS else {}
    for no, lines in enumerate(pages, 1):
        for j, ln in enumerate(lines):
            if not ln.strip():
                continue
            if (no, j) in heads:
                doc.blocks.append(B.Block(B.HEADING, ln.strip(), level=heads[(no, j)], page=no,
                                          source=B.SRC_INFERRED))
                continue
            kind = edge.get((no, j), B.PARAGRAPH)
            doc.blocks.append(B.Block(kind, ln.strip(), page=no))
    return doc


#------------------------------------------------------------------
# 줄마다 글자 크기 (P-2)
#=> 쪽 글자를 _LINE_SPLIT 과 같은 규칙으로 줄로 나누며, 줄마다 공백이 아닌 글자들의
#   글꼴 크기 중앙값을 낸다. 글자 수(count_chars)와 글 길이가 다르면 위치를 맞출 수 없어
#   그 쪽은 크기 없이 둔다.
#
# -in: tp      = PdfTextPage
# -in: txt     = tp.get_text_range() 결과
# -in: n_lines = _LINE_SPLIT 으로 나눈 줄 수(맞춰 볼 기준)
#
# -out: list[float 또는 None] — 줄마다 글꼴 크기 (모르면 None)
# -out: error = 없음 (pdfium 호출 실패 시 모두 None)
#------------------------------------------------------------------
def _line_sizes(tp, txt, n_lines):
    try:
        if tp.count_chars() != len(txt or ""):
            return [None] * n_lines
        raw = pdfium.raw
        out, cur = [], []
        i = 0
        while i < len(txt):
            ch = txt[i]
            if ch in "\r\n":
                out.append(statistics.median(cur) if cur else None)
                cur = []
                # \r\n 은 줄바꿈 하나 — _LINE_SPLIT 과 같은 규칙
                i += 2 if (ch == "\r" and i + 1 < len(txt) and txt[i + 1] == "\n") else 1
                continue
            if not ch.isspace():
                cur.append(round(raw.FPDFText_GetFontSize(tp.raw, i), 1))
            i += 1
        out.append(statistics.median(cur) if cur else None)
        return out if len(out) == n_lines else [None] * n_lines
    except Exception:           # noqa: BLE001 — 크기를 못 읽으면 제목 추정만 하지 않는다
        return [None] * n_lines


#------------------------------------------------------------------
# 글자 크기로 제목 줄 추정 (P-2)
#=> 본문보다 확연히 큰 짧은 줄을 제목으로 본다.
#    1) 본문 크기 = 15자 이상 줄들의 크기 최빈값(글자 수 가중)
#    2) 크기 ≥ 본문 × HEAD_RATIO, 길이 ≤ HEAD_MAX_CHARS, 머리말·쪽번호·목차 아님 → 후보
#    3) 후보가 MIN_INFERRED 개 미만이거나 전체 줄의 HEAD_MAX_SHARE 를 넘으면 추정하지 않는다
#       (크기 정보가 없는 PDF, 문서 전체가 큰 글씨인 PDF)
#    4) 후보 크기를 큰 순으로 순위 매겨 수준 1~MAX_LEVEL
#   굵기만으로는 정하지 않는다 — 실측에서 Q/A 강조(상담사례집)에 굵기를 썼다.
#
# -in: pages = 쪽별 줄 목록
# -in: sizes = 쪽별 줄 크기 목록(_line_sizes)
# -in: edge  = _edge_kinds 결과(제외할 가장자리 줄)
#
# -out: dict = {(쪽, 줄 위치): 수준}
# -out: error = 없음
#------------------------------------------------------------------
def _infer_headings(pages, sizes, edge):
    weight = {}
    n_lines = 0
    for lines, szs in zip(pages, sizes):
        for ln, s in zip(lines, szs):
            if s and len(ln.strip()) >= 15:
                weight[s] = weight.get(s, 0) + len(ln.strip())
            if ln.strip():
                n_lines += 1
    if not weight:
        return {}
    body = max(weight, key=weight.get)
    cand = {}
    for no, (lines, szs) in enumerate(zip(pages, sizes), 1):
        for j, (ln, s) in enumerate(zip(lines, szs)):
            t = ln.strip()
            if not t or not s or (no, j) in edge or len(t) > HEAD_MAX_CHARS:
                continue
            if s >= body * HEAD_RATIO and re.search(r"[가-힣A-Za-z]", t) and not _TOC_TAIL.search(t):
                cand[(no, j)] = s
    if len(cand) < MIN_INFERRED or len(cand) > n_lines * HEAD_MAX_SHARE:
        return {}
    ranks = sorted({round(s / body, 1) for s in cand.values()}, reverse=True)
    level = {r: min(MAX_LEVEL, k + 1) for k, r in enumerate(ranks)}
    return {pos: level[round(s / body, 1)] for pos, s in cand.items()}


#------------------------------------------------------------------
# 쪽 가장자리 줄의 종류 정하기 (머리말·꼬리말·쪽번호)
#=> 쪽마다 맨 위·아래 EDGE_LINES 줄(빈 줄 제외)을 본다.
#    1) 쪽번호 꼴이면 page_number
#    2) 숫자를 '#' 로 바꾼 글이 같은 가장자리에서 MIN_REPEAT_PAGES 쪽 이상 되풀이되면
#       header(위) / footer(아래). "Ⅰ. 적용대상 11" 과 "Ⅰ. 적용대상 12" 가 같은 줄로 묶인다
#   '전체 쪽의 절반' 같은 비율 기준은 쓰지 않는다 — 실측에서 짝수 쪽에만 찍히는 책 제목,
#   장마다 바뀌는 장 제목 머리말이 모두 비율 아래였다(청탁금지법 해설집·상담사례집).
#   대신 **처음 나온 1개는 본문에 남긴다** — 쪽마다 반복되는 표 머리글 행이 통째로 사라지지 않게.
#
# -in: pages = [[줄, …], …] 쪽별 줄 목록
#
# -out: dict = {(쪽 번호 1부터, 줄 위치): 블록 종류}
# -out: error = 없음
#------------------------------------------------------------------
def _edge_kinds(pages):
    out = {}
    tops, bottoms = {}, {}
    for no, lines in enumerate(pages, 1):
        idx = [j for j, ln in enumerate(lines) if ln.strip()]
        for zone, picks in (("top", idx[:EDGE_LINES]), ("bottom", idx[-EDGE_LINES:])):
            for j in picks:
                s = lines[j].strip()
                if _PAGE_NO.match(s):
                    out[(no, j)] = B.PAGE_NUMBER
                    continue
                key = re.sub(r"\d+", "#", re.sub(r"\s+", " ", s))
                # 긴 줄은 본문이다. 숫자를 빼면 거의 남는 게 없는 짧은 줄("제1조", "Q1.")은
                # 숫자만 다른 본문 줄끼리 같은 줄로 묶여 머리말로 오판된다 — 후보에서 뺀다
                if len(s) > MAX_HEADER_CHARS or len(re.sub(r"[#\W_]+", "", key)) < MIN_HEADER_LETTERS:
                    continue
                bucket = tops if zone == "top" else bottoms
                bucket.setdefault(key, []).append((no, j))
    for bucket, kind in ((tops, B.HEADER), (bottoms, B.FOOTER)):
        for key, spots in bucket.items():
            if len({no for no, _ in spots}) >= MIN_REPEAT_PAGES:
                # 처음 나온 자리는 본문으로 남기고 나머지만 표시한다
                for spot in sorted(set(spots))[1:]:
                    out.setdefault(spot, kind)
    return out
