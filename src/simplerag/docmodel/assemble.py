#------------------------------------------------------------------
# 블록 → 절 조립 (재설계서 §11)
#=> 제목 블록의 수준으로 '제목 경로 스택'을 쌓아 가며 블록을 절로 묶는다.
#
#     제목 경로 스택 = []
#     heading(level=n) → 스택을 n-1 깊이까지 줄이고 이 제목을 push → 새 절 시작
#     그 외           → 현재 절에 추가, block.path = 스택 복사
#
#   예) 제3장 근무 (L1) > 제12조(근로시간) (L2) > 본문·표 …
#       → 절 path = ["제3장 근무", "제12조(근로시간)"]
#
#   자르는 일(토큰 예산)은 chunk.split_blocks 가 한다. 여기는 '어디까지가 한 절인가'만 정한다.
#------------------------------------------------------------------

import re
from dataclasses import dataclass, field

from .. import structure
from . import blocks as B


#------------------------------------------------------------------
# 절 하나
#
# -필드: id        = 문서 안 절 번호(0부터). payload section_id 로 쓴다
# -필드: path      = 제목 경로 이름표 목록 (머리말 절은 [])
# -필드: heading   = 절을 연 제목 블록(머리말 절이면 None)
# -필드: blocks    = 절 본문 블록 목록(제목 블록 제외, 제외 블록 제외)
# -필드: page_from, page_to = 이 절이 걸친 쪽 범위(모르면 None)
#------------------------------------------------------------------
@dataclass
class Section:
    id: int
    path: list
    heading: object = None
    blocks: list = field(default_factory=list)
    page_from: int = None
    page_to: int = None


#------------------------------------------------------------------
# 제목 글자 → 경로 이름표
#=> 기존 structure._label 과 같은 규칙이다. 번호절 문서에서는 제목 줄이 곧 내용인
#   경우가 많아("14.2 가족이 유해를 운구할 경우 …") 통째로 이름표에 넣으면 접두가
#   길어진다. 긴 줄은 번호만 이름표로 쓰고, 번호도 없으면 앞부분만 줄여 쓴다.
#   (제목 글자 자체는 청크 본문에 그대로 남으므로 내용은 사라지지 않는다.)
#
# -in: text = 제목 블록 글자
#
# -out: str 이름표 또는 None(쓸 만한 이름표가 없음)
# -out: error = 없음
#------------------------------------------------------------------
def label_of(text):
    flat = " ".join((text or "").split())
    if not flat:
        return None
    if len(flat) <= structure._TITLE_MAX:
        return flat.lstrip("#").strip() or None
    m = structure._NUMBERING.match(flat)
    if m:
        return m.group(1).strip()
    # 번호 없는 긴 제목 — 이름표로는 앞부분이면 충분하다
    return flat[:structure._TITLE_MAX].rstrip() + "…"


#------------------------------------------------------------------
# 절 조립 (핵심)
#=> 블록을 읽기 순서로 돌며 제목이 나올 때마다 절을 새로 연다.
#    1) 제외 블록(머리말·꼬리말·쪽번호·목차)은 건너뛴다 — 청크에 넣지 않는다
#    2) 첫 제목 앞의 블록은 경로 없는 '머리말 절'로 모은다(문서 제목·제정일 등)
#    3) 제목이 나오면 스택을 그 수준-1 깊이까지 줄이고 push 한 뒤 새 절을 연다
#    4) 블록마다 path 를 채우고, 절의 쪽 범위를 넓힌다
#   본문이 없는 절(제목만 있고 바로 하위 제목이 오는 경우)도 남긴다 — 제목 글자가
#   청크에 들어가야 '제3장 근무' 같은 장 제목으로도 검색이 걸린다.
#
# -in: doc = postprocess.finalize 를 거친 Document (블록 경로)
#
# -out: [Section, …] 문서 순서
# -out: error = 없음
#------------------------------------------------------------------
def build_sections(doc):
    sections = []
    stack = []                 # [(level, label), …]
    cur = Section(id=0, path=[])

    # 지금 절을 목록에 넣는다(머리말 절이 비어 있으면 버린다)
    def close():
        if cur.heading is not None or cur.blocks:
            cur.id = len(sections)
            sections.append(cur)

    for b in doc.blocks:
        if b.type in B.EXCLUDED:
            continue
        if b.type == B.HEADING:
            close()
            level = max(1, b.level or 1)
            # 같거나 더 높은 수준의 제목을 만나면 그 아래 경로는 끝난다
            while stack and stack[-1][0] >= level:
                stack.pop()
            label = b.text.strip() if b.synthetic else label_of(b.text)
            stack.append((level, label))
            path = [lab for _, lab in stack if lab]
            b.path = list(path)
            cur = Section(id=0, path=path, heading=b,
                          page_from=b.page, page_to=b.page)
            continue
        b.path = list(cur.path)
        cur.blocks.append(b)
        if b.page is not None:
            cur.page_from = b.page if cur.page_from is None else min(cur.page_from, b.page)
            cur.page_to = b.page if cur.page_to is None else max(cur.page_to, b.page)
    close()
    return sections


#------------------------------------------------------------------
# 절 본문의 '단위' 목록 — 청킹기가 자르는 최소 단위
#=> 블록 하나가 단위 하나다. 단 표는 **행 하나가 단위**다 — 큰 표를 행 경계에서
#   끊고 조각마다 머리글 행을 다시 붙여야 하기 때문이다.
#    · 제목 블록  → 단위 1개 (합성 제목이면 본문에 넣지 않으므로 생략)
#    · 표        → 머리글 행 묶음 1개 + 값 행마다 1개 (값 행 단위에는 머리글을 함께 적는다)
#    · 그 외      → 블록 글자 1개
#
# -in: sec = Section
#
# -out: [(글자, 머리글 또는 None), …]
# -out: error = 없음
#------------------------------------------------------------------
def section_units(sec):
    units = []
    if sec.heading is not None and not sec.heading.synthetic and sec.heading.text.strip():
        units.append((sec.heading.text.strip(), None))
    for b in sec.blocks:
        if b.type == B.TABLE and b.rows:
            lines = b.text.split("\n")
            nh = b.header_rows or 0
            header = "\n".join(lines[:nh]) if nh else None
            if header:
                units.append((header, None))
            for ln in lines[nh:]:
                units.append((ln, header))
            continue
        if b.text and b.text.strip():
            units.append((_strip_blank_lines(b.text), None))
    return units


#------------------------------------------------------------------
# 블록 글자의 빈 줄 정리
#
# -in: s = 블록 글자
#
# -out: str = 앞뒤 공백과 빈 줄을 걷어 낸 글자
# -out: error = 없음
#------------------------------------------------------------------
def _strip_blank_lines(s):
    return "\n".join(ln.rstrip() for ln in s.strip().split("\n") if ln.strip())


#------------------------------------------------------------------
# 청크 앞에 붙일 출처 경로 (기존 structure.make_prefix 의 경로판)
#=> "[문서 > 상위 제목 > 절 제목]" 한 줄. 경로가 길면 가운데부터 '…' 로 줄인다 —
#   문서명과 가장 가까운 절 제목이 검색·생성 양쪽에 가장 쓸모 있다.
#
# -in: title    = 문서 제목(없으면 None)
# -in: path     = 제목 경로 목록
# -in: max_chars = 접두 글자 상한(기본 80)
#
# -out: str = "[a > b > c]\n" 또는 ""(붙일 것이 없음)
# -out: error = 없음
#------------------------------------------------------------------
def make_prefix(title, path, max_chars=80):
    parts = [p for p in ([title] if title else []) + list(path or []) if p]
    if not parts:
        return ""
    # 가운데 경로를 하나씩 빼면서 길이를 맞춘다 — 처음(문서)과 끝(절)은 지킨다
    while len(" > ".join(parts)) > max_chars and len(parts) > 2:
        mid = len(parts) // 2
        if parts[mid] == "…":
            mid = mid - 1 if mid - 1 > 0 else mid + 1
            if mid >= len(parts) - 1:
                break
        parts[mid] = "…"
        # 연달아 생긴 '…' 은 하나로 합친다
        parts = [p for i, p in enumerate(parts) if not (p == "…" and i and parts[i - 1] == "…")]
    return "[" + re.sub(r"\s+", " ", " > ".join(parts)) + "]\n"
