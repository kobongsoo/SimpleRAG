#------------------------------------------------------------------
# 블록 모델 (plan/구조화추출_청킹_재설계서.html §6)
#=> 추출 결과를 '한 덩어리 문자열'이 아니라 **종류·제목 수준·쪽이 붙은 블록 목록**으로
#   담는 그릇이다. 포맷별 추출기는 모두 이 형태로 결과를 내고, 청킹기는 이 형태만 받는다.
#   그래서 포맷이 늘어도 청킹 코드는 하나로 유지된다.
#------------------------------------------------------------------

from dataclasses import dataclass, field

# 블록 종류 — 청킹에서 어떻게 다룰지가 종류로 정해진다(§6 표).
HEADING = "heading"
PARAGRAPH = "paragraph"
LIST_ITEM = "list_item"
TABLE = "table"
CAPTION = "caption"
CODE = "code"
HEADER = "header"            # 머리말 — 청크에서 제외
FOOTER = "footer"            # 꼬리말 — 청크에서 제외
PAGE_NUMBER = "page_number"  # 쪽번호 — 청크에서 제외
TOC = "toc"                  # 목차 — 청크에서 제외(목차가 근거로 끌려오는 문제 차단)
NOTE = "note"                # 발표자 노트·각주·셀 메모 — 본문 뒤 별도 절
FIGURE_TEXT = "figure_text"  # 그림 속 글자(OCR) — 지금은 쓰지 않음

# 청크에 넣지 않는 종류. 지우지 않고 표시만 해서 진단 JSON 에는 남긴다.
EXCLUDED = frozenset({HEADER, FOOTER, PAGE_NUMBER, TOC})

# 출처 — 진단·평가에서 출처별 성능을 가르기 위해 적는다.
SRC_PARSER = "parser"        # 파일 안 구조를 그대로 읽음
SRC_INFERRED = "inferred"    # 서식(글자 크기 등)으로 추정
SRC_REGEX = "regex"          # 본문 텍스트에 정규식(structure.py)으로 추측
SRC_OCR = "ocr"


#------------------------------------------------------------------
# 블록 하나
#=> 문서의 한 조각(제목·문단·표·목록 항목 …)이다. 읽기 순서대로 Document.blocks 에 쌓인다.
#
# -필드: type        = 블록 종류(위 상수)
# -필드: text        = 블록 글자. 표는 rows 를 렌더링한 글이 postprocess 에서 채워진다
# -필드: level       = 제목 수준 1~9(heading) · 목록 깊이(list_item). 그 밖은 0
# -필드: rows        = 표만 — [[셀, …], …]. 병합 셀은 펼쳐 같은 글자로 채운다
# -필드: header_rows = 표만 — 머리글 행 수(파일이 표시한 값, 없으면 추정, 모르면 0)
# -필드: page        = 시작 쪽/슬라이드/시트 번호(1부터). 쪽 개념이 없으면 None
# -필드: path        = 이 블록이 속한 제목 경로 — 절 조립(assemble) 단계가 채운다
# -필드: source      = parser | inferred | regex | ocr
# -필드: synthetic   = True 면 문서에 없는 글자다(예: 제목 없는 슬라이드의 "슬라이드 3").
#                      제목 경로에는 쓰되 청크 본문에는 넣지 않는다
#------------------------------------------------------------------
@dataclass
class Block:
    type: str
    text: str = ""
    level: int = 0
    rows: list = None
    header_rows: int = 0
    page: int = None
    path: list = field(default_factory=list)
    source: str = SRC_PARSER
    synthetic: bool = False

    #------------------------------------------------------------------
    # 진단 JSON 용 사전으로 바꾸기
    #=> 기본값인 필드는 빼서 사람이 읽기 쉽게 만든다.
    #
    # -in: 없음
    #
    # -out: dict = 값이 있는 필드만 담은 사전
    # -out: error = 없음
    #------------------------------------------------------------------
    def to_dict(self):
        d = {"type": self.type, "text": self.text}
        # 비어 있거나 기본값인 필드는 생략 — 수천 블록짜리 JSON 을 눈으로 볼 수 있게
        if self.level:
            d["level"] = self.level
        if self.rows is not None:
            d["rows"] = self.rows
            d["header_rows"] = self.header_rows
        if self.page is not None:
            d["page"] = self.page
        if self.path:
            d["path"] = self.path
        if self.source != SRC_PARSER:
            d["source"] = self.source
        if self.synthetic:
            d["synthetic"] = True
        return d


#------------------------------------------------------------------
# 문서 하나
#=> 블록 목록과 문서 정보를 담는다. 구조 파서가 없는 포맷(TXT·DOC 등)은 blocks 대신
#   raw_text 에 기존 추출 결과를 담는다 — 그 경로는 기존 청킹기(split_structured)를
#   그대로 타서 지금과 글자까지 같은 청크를 낸다(§11 회귀 기준).
#
# -필드: format   = "docx" | "pptx" | "xlsx" | "hwpx" | "html" | "md" | "ipynb" | "legacy"
# -필드: title    = 문서 제목(파일명에서 확장자를 뗀 것)
# -필드: blocks   = [Block, …] 읽기 순서
# -필드: raw_text = 폴백 경로의 평문(블록 경로면 None)
# -필드: pages    = 쪽/슬라이드/시트 수(모르면 None)
# -필드: warnings = 추출 중 남길 경고 문장 목록
#------------------------------------------------------------------
@dataclass
class Document:
    format: str
    title: str = ""
    blocks: list = field(default_factory=list)
    raw_text: str = None
    pages: int = None
    warnings: list = field(default_factory=list)

    #------------------------------------------------------------------
    # 블록 경로인가
    #
    # -in: 없음
    #
    # -out: bool = 블록 목록으로 청킹하면 True, 폴백 평문이면 False
    # -out: error = 없음
    #------------------------------------------------------------------
    @property
    def is_blocks(self):
        return self.raw_text is None

    #------------------------------------------------------------------
    # 문서 전체 글자 — 진단·회귀 경고용
    #=> 블록 경로면 제외 블록까지 포함한 모든 블록 글자를, 폴백이면 평문을 돌려준다.
    #
    # -in: 없음
    #
    # -out: str = 문서 전체 글자
    # -out: error = 없음
    #------------------------------------------------------------------
    def full_text(self):
        if not self.is_blocks:
            return self.raw_text or ""
        return "\n".join(b.text for b in self.blocks if b.text and not b.synthetic)

    #------------------------------------------------------------------
    # 진단 JSON 용 사전
    #
    # -in: 없음
    #
    # -out: dict = 문서 정보 + 블록 사전 목록
    # -out: error = 없음
    #------------------------------------------------------------------
    def to_dict(self):
        return {"format": self.format, "title": self.title, "pages": self.pages,
                "warnings": self.warnings,
                "raw_text": self.raw_text if not self.is_blocks else None,
                "blocks": [b.to_dict() for b in self.blocks]}
