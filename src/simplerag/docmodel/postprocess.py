#------------------------------------------------------------------
# 블록 후처리 (재설계서 §10)
#=> 포맷별 추출기가 낸 블록 목록을 청킹 직전 모양으로 다듬는다. 모든 포맷이 같은
#   규칙을 타도록 한곳에 모았다.
#    1) 정제 — 토크나이저를 죽이는 문자 제거(extract.sanitize 재사용)
#    2) 표 정리 — 한 칸짜리 표(글상자)는 문단으로 되돌리고, 나머지는 셀을 한 줄로
#       다듬어 "| a | b |" 로 렌더링, 머리글 행 수 확정
#    3) 목차 표시 — 점선 리더 뒤 쪽번호 줄이 이어지면 toc 로 표시(청크에서 제외)
#    4) 제목 폴백 — 파일이 알려 준 제목이 너무 적으면 기존 정규식(structure.py)으로 추측
#    5) 무손실 검사 — 제외된 글자가 지나치게 많으면 평문 폴백으로 돌린다
#------------------------------------------------------------------

import re

from .. import structure
from ..extract import sanitize
from . import blocks as B
from . import tables

# 파일이 알려 준 제목이 이보다 적으면 정규식 추측으로 물러난다(structure._MIN_HEADINGS 와 같은 값)
MIN_PARSER_HEADINGS = 3
# 제외 블록을 뺀 본문이 전체의 이 비율보다 작으면 무언가 잘못 표시된 것으로 본다
MIN_KEPT_RATIO = 0.7

# 목차 줄 — 점선·가운데점 리더 뒤에 쪽번호로 끝난다("제1장 총칙 ········ 3")
_TOC_LINE = re.compile(r"(?:\.{3,}|·{3,}|…{2,}|-{4,}|_{4,}|\s{6,})\s*\d{1,4}\s*$")
_MIN_TOC_RUN = 3          # 이만큼 연달아 나와야 목차로 본다(본문 속 우연한 한 줄 방어)

_PARA_LIKE = (B.PARAGRAPH, B.LIST_ITEM)
# 슬라이드·시트 단위가 곧 절인 포맷 — 제목 폴백을 하지 않는다
NO_REGEX_FORMATS = ("pptx", "xlsx")
# 눕힌 행(한 칸에 여러 행을 줄바꿈으로 넣은 표)을 펼치는 포맷 — 기존 경로와 같게 DOC 만.
# DOCX·HWPX 는 한 칸에 여러 문단을 넣은 정상 표(긴 설명 칸)가 많아 잘못 펼칠 위험이 크다
STACKED_ROW_FORMATS = ("doc",)


#------------------------------------------------------------------
# 후처리 진입점
#=> 위 다섯 단계를 차례로 적용한다. 문서는 제자리에서 바뀐다.
#
# -in: doc = Document (블록 경로)
#
# -out: doc = 같은 객체(편의상 돌려준다)
# -out: error = 없음 (이상이 있으면 doc.warnings 에 적고 평문 폴백으로 돌린다)
#------------------------------------------------------------------
def finalize(doc):
    if not doc.is_blocks:
        return doc
    _sanitize(doc)
    _tidy_tables(doc)
    # 빈 블록은 청크 경계 계산만 흐린다 — 합성 제목(슬라이드 번호 등)만 남긴다
    doc.blocks = [b for b in doc.blocks if b.synthetic or (b.text and b.text.strip())
                  or (b.type == B.TABLE and b.rows)]
    _mark_toc(doc)
    _heading_fallback(doc)
    _lossless_check(doc)
    return doc


#------------------------------------------------------------------
# 1) 정제
#=> 블록 글자와 표 셀마다 sanitize 를 건다. 비문자·사용자정의영역·짝 없는
#   서로게이트가 남으면 토크나이저(Rust)가 죽어 인덱싱 전체가 멈춘다(설계서 R10).
#
# -in: doc = Document
#
# -out: 없음 (제자리 수정)
# -out: error = 없음
#------------------------------------------------------------------
def _sanitize(doc):
    for b in doc.blocks:
        if b.text:
            b.text, _ = sanitize(b.text)
        if b.rows:
            b.rows = [[sanitize(c)[0] if c else c for c in row] for row in b.rows]


#------------------------------------------------------------------
# 2) 표 정리
#=> 한글·워드 문서는 표를 **글상자**로 많이 쓴다(보도자료의 요약 상자 등). 한 칸짜리
#   표를 "| 긴 문단 |" 으로 두면 표 행 분할·머리글 재부착이 엉뚱하게 걸리므로 문단으로
#   되돌린다. 나머지 표는 셀을 한 줄로 다듬고 렌더링한다.
#
# -in: doc = Document
#
# -out: 없음 (doc.blocks 를 새 목록으로 바꾼다)
# -out: error = 없음
#------------------------------------------------------------------
def _tidy_tables(doc):
    out = []
    for b in doc.blocks:
        if b.type != B.TABLE:
            out.append(b)
            continue
        rows = b.rows or []
        if not rows:
            continue
        width = max(len(r) for r in rows)
        if width <= 1:
            # 한 칸짜리 표 — 셀 안 줄을 그대로 문단으로 펼친다(글자 손실 없음)
            for row in rows:
                for line in (row[0] if row else "").split("\n"):
                    if line.strip():
                        out.append(B.Block(B.PARAGRAPH, line.strip(), page=b.page,
                                           source=b.source))
            continue
        if doc.format in STACKED_ROW_FORMATS:
            rows, b.header_rows = _expand_stacked(rows, b.header_rows)
        b.rows = [[tables.clean_cell(c) for c in row] for row in rows]
        b.header_rows = tables.header_rows(b.rows, b.header_rows)
        b.text = tables.render(b.rows)
        out.append(b)
    doc.blocks = out


#------------------------------------------------------------------
# 눕힌 행 펼치기 (DOC 표)
#=> 한글에서 변환된 DOC 표는 여러 행을 한 칸 안에 줄바꿈으로 눕혀 넣은 경우가 많다
#   (경조사지원규정: "본인 결혼\n자녀 결혼\n…" | "5\n1\n…" | "500,000\n300,000\n…").
#   칸을 공백으로 이으면 행과 값의 대응이 깨진다. 기존 경로의 extract._expand_row 규칙을
#   그대로 쓴다 — 줄이 3줄 이상 쌓인 칸이 2개 이상이면 줄끼리 짝지어 행으로 펼치고,
#   한 줄짜리 칸(그룹 이름표 "경 사")은 모든 행에 되풀이한다(REPORT §20.6).
#   머리글 행이 펼쳐지면 그만큼 머리글 행 수도 늘린다.
#
# -in: rows     = 표 행렬(칸 안 줄바꿈 유지)
# -in: n_header = 머리글 행 수
#
# -out: (rows, n_header) — 펼친 행렬과 고친 머리글 행 수
# -out: error = 없음 (펼칠 수 없는 행은 그대로 둔다)
#------------------------------------------------------------------
def _expand_stacked(rows, n_header):
    from ..extract import _expand_row

    out, new_header = [], 0
    for i, row in enumerate(rows):
        lines = [c.split("\n") for c in row]
        stacked = [len(ls) for ls in lines if len(ls) >= 3]
        expanded = _expand_row(list(row)) if len(stacked) >= 2 else []
        part = expanded or [row]
        out.extend(part)
        if i < n_header:
            new_header += len(part)
    return out, new_header


#------------------------------------------------------------------
# 3) 목차 표시
#=> 목차는 모든 절 제목을 한곳에 모아 두어 BM25·dense 양쪽에서 점수가 높지만 답이
#   없다(Newploy 보고서: 목차 쪽이 근거로 자주 끌려와 적중률 약 55%). 리더 줄이
#   _MIN_TOC_RUN 개 이상 이어지면 그 줄들을 toc 로 표시해 청크에서 뺀다.
#
# -in: doc = Document
#
# -out: 없음 (블록 type 을 제자리에서 바꾼다)
# -out: error = 없음
#------------------------------------------------------------------
def _mark_toc(doc):
    run = []

    # 모아 둔 연속 줄이 충분히 길면 목차로 확정한다
    def close():
        if len(run) >= _MIN_TOC_RUN:
            for blk in run:
                blk.type = B.TOC
        run.clear()

    for b in doc.blocks:
        if b.type in _PARA_LIKE + (B.HEADING,) and not b.synthetic \
                and _TOC_LINE.search(b.text.strip()):
            run.append(b)
            continue
        close()
    close()


#------------------------------------------------------------------
# 4) 제목 폴백
#=> 국내 문서는 제목을 스타일 대신 '굵게·크게' 직접 서식으로 만드는 일이 흔해,
#   파일이 알려 주는 제목이 거의 없을 때가 많다. 그때는 기존 정규식 추측
#   (조문·마크다운·번호절 중 하나 + 부속서 표지)을 블록 글자에 적용한다.
#    1) 파일 제목(합성 제외)이 MIN_PARSER_HEADINGS 이상이면 그대로 쓴다
#    2) 아니면 그 몇 개는 문단으로 내리고(글자는 남는다) 정규식으로 제목을 찾는다
#       — 파서 제목과 정규식 제목을 섞지 않는다(문서마다 문법 하나, detect_grammar 원칙)
#    3) 정규식도 아무것도 못 찾으면 내렸던 파일 제목을 되살린다
#
# -in: doc = Document
#
# -out: 없음 (블록을 제자리에서 바꾸거나 나눈다)
# -out: error = 없음
#------------------------------------------------------------------
def _heading_fallback(doc):
    # 슬라이드·시트가 곧 절인 포맷은 정규식 제목을 얹지 않는다 — 슬라이드 안의 "1. …" 이
    # 수준 1 제목으로 잡히면 슬라이드 경계가 깨진다
    if doc.format in NO_REGEX_FORMATS:
        return
    parser_heads = [b for b in doc.blocks
                    if b.type == B.HEADING and not b.synthetic and b.source == B.SRC_PARSER]
    if len(parser_heads) >= MIN_PARSER_HEADINGS:
        return
    # 블록은 dataclass 라 == 가 내용 비교다 — 같은 글자의 다른 블록과 헷갈리지 않게 id 로 가른다
    head_ids = {id(b) for b in parser_heads}

    # 정규식 문법은 '문단처럼 생긴' 블록 글자만 보고 고른다 — 표 셀 글자가 섞이면 오판한다
    candidates = [b for b in doc.blocks if b.type in _PARA_LIKE or id(b) in head_ids]
    text = "\n".join(b.text for b in candidates)
    name, pat = structure.detect_grammar(text)
    n_app = len(structure._APPENDIX.findall(text))
    if not name and n_app < structure._MIN_APPENDIX:
        return
    # 부속서 표지(별표·부칙 …)가 있으면 그것을 최상위(수준 1)로 두고 문법 제목은 한 단계
    # 내린다 — 그래야 '부칙 > 제1조(시행일)' 처럼 부속서 아래 조문이 부모 경로를 갖는다
    offset = 1 if (name and n_app) else 0

    out = []
    for b in doc.blocks:
        if b.type not in _PARA_LIKE and id(b) not in head_ids:
            out.append(b)
            continue
        first, _, rest = b.text.partition("\n")
        level = _regex_level(name, pat, first, offset)
        if level is None:
            # 파일 제목이었지만 정규식에 안 걸리면 문단으로 내린다(글자는 그대로)
            if id(b) in head_ids:
                b.type, b.level = B.PARAGRAPH, 0
            out.append(b)
            continue
        out.append(B.Block(B.HEADING, first.strip(), level=level, page=b.page,
                           source=B.SRC_REGEX))
        if rest.strip():
            # 제목 줄 뒤에 같은 문단으로 이어 붙은 줄은 본문으로 남긴다
            out.append(B.Block(b.type, rest.strip(), level=b.level, page=b.page,
                               source=b.source))
    doc.blocks = out


#------------------------------------------------------------------
# 정규식 제목 줄의 수준 정하기
#=> 문법마다 깊이를 읽는 방법이 다르다.
#    · 마크다운 — '#' 개수
#    · 번호절   — "1.2.3" 의 숫자 개수
#    · 조문     — 1
#    · 부속서 표지(별표·붙임·부칙 …) — 1 (최상위 구분)
#   부속서 표지가 있는 문서는 문법 제목에 offset(1)을 더해 부속서 아래로 들어가게 한다.
#
# -in: name   = detect_grammar 가 고른 문법 이름(없으면 None)
# -in: pat    = 그 문법 정규식(없으면 None)
# -in: line   = 블록 첫 줄
# -in: offset = 문법 제목에 더할 수준(기본 0)
#
# -out: int 수준(1~9) 또는 None(제목 줄이 아님)
# -out: error = 없음
#------------------------------------------------------------------
def _regex_level(name, pat, line, offset=0):
    s = line.rstrip()
    if pat is not None and pat.fullmatch(s):
        if name == "마크다운":
            return min(9, len(s.strip()) - len(s.strip().lstrip("#")) + offset)
        if name == "번호절":
            m = re.match(r"\s*(\d+(?:\.\d+)*)", s)
            return min(9, (len(m.group(1).split(".")) if m else 1) + offset)
        return 1 + offset
    if structure._APPENDIX.fullmatch(s):
        return 1
    return None


#------------------------------------------------------------------
# 5) 무손실 검사
#=> 후처리는 글자를 '지우지' 않고 '표시'만 한다. 그래도 표시가 잘못되면(예: 본문
#   대부분이 목차로 잡힘) 청크에서 내용이 통째로 빠진다. normalize_tables 의 93% 손실
#   사고(ARIA-WISC.pdf)에서 얻은 원칙대로, 결과를 직접 재서 이상하면 평문 폴백으로 돌린다.
#
# -in: doc = Document
#
# -out: 없음 (이상 시 doc.raw_text 를 채워 폴백으로 바꾼다)
# -out: error = 없음
#------------------------------------------------------------------
def _lossless_check(doc):
    def size(bs):
        return sum(len(re.sub(r"\s+", "", b.text or "")) for b in bs if not b.synthetic)

    total = size(doc.blocks)
    if total == 0:
        # 글자가 하나도 없다 — 상위가 '추출 텍스트 없음'으로 처리하게 빈 평문으로 둔다
        doc.raw_text = ""
        return
    kept = size(b for b in doc.blocks if b.type not in B.EXCLUDED)
    if kept < total * MIN_KEPT_RATIO:
        doc.warnings.append("제외 블록이 본문의 {:.0%} — 표시 오류로 보고 평문 폴백".format(
            1 - kept / total))
        doc.raw_text = doc.full_text()
