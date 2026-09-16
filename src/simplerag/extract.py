#------------------------------------------------------------------
# 텍스트 추출 계층 (설계서 결정11 + R11 표 보존)
#=> CSOClassify 의 extract 모듈을 재사용하되, 두 가지를 SimpleRAG 쪽에서 보정한다.
#
#   보정 1 — OLE 포맷 오판 우회
#     CSOClassify detect_format 은 파일 앞 2048바이트를 읽어 놓고
#       if head == _OLE_MAGIC:      # ← 버퍼 '전체'를 8바이트 매직과 비교
#     로 검사한다. 절대 참이 될 수 없어 모든 OLE 문서(.doc/.xls/.ppt/.hwp)가
#     'binary' 로 판정되고, 전용 파서를 건너뛴 채 사이냅 폴백으로 넘어간다.
#     (PDF·ZIP 은 head[:5], head[:4] 로 슬라이스하는데 OLE 만 빠졌다)
#     → 여기서 head[:8] 로 다시 판정해 전용 파서를 타게 한다.
#
#   보정 2 — 표 구조 복원
#     .doc 전용 파서(DocExtractor)는 셀 구분자 0x07 을 탭으로 남겨 표 구조를
#     보존한다. 반면 사이냅 출력은 셀을 줄바꿈으로 흩어 놓아 레이블-값 대응이
#     끊긴다. 보정 1 로 전용 파서를 타게 되면 탭이 살아나므로, 그 탭 표를
#     마크다운 표로 정규화한다.
#
#     실측 영향: 경조사지원규정(표 문서) 질문 정답률 25% → 아래에서 재측정.
#------------------------------------------------------------------

import json
import os
import re
import sys

from . import config


#------------------------------------------------------------------
# CSOClassify extract 모듈 적재
#=> 복사하지 않고 sys.path 에 얹어 그대로 재사용한다.
#
# -out: csoclassify.extract 모듈
# -out: error = 경로/임포트 실패 시 RuntimeError
#------------------------------------------------------------------
def _load_cso():
    # exe 로 빌드하면 csoclassify.extract 가 함께 번들되므로 경로 없이 바로 import
    # 된다. 소스 실행일 때만 CSO_SRC 를 sys.path 에 얹는다.
    if os.path.isdir(config.CSO_SRC) and config.CSO_SRC not in sys.path:
        sys.path.insert(0, config.CSO_SRC)

    try:
        import csoclassify.extract as cso_extract
        return cso_extract
    except ImportError as e:
        raise RuntimeError(
            "텍스트 추출 모듈(csoclassify.extract)을 찾을 수 없습니다: {}\n"
            "        소스 실행: 환경변수 SIMPLERAG_CSO_SRC 로 CSOClassify\\src 경로를 지정하세요.\n"
            "        현재 설정값: {}".format(e, config.CSO_SRC))


#------------------------------------------------------------------
# 포맷 감지 (OLE 오판 보정)
#=> OLE 매직이면 내부 스트림으로 세부 구분(doc/xls/ppt/hwp)한다.
#   그 외는 원본 감지 결과를 그대로 쓴다.
#
# -in: path
#
# -out: 감지 타입 문자열
#------------------------------------------------------------------
def make_detector():
    cso = _load_cso()
    from csoclassify.extract import detect as D

    def detect(path):
        try:
            with open(path, "rb") as f:
                head = f.read(8)
        except OSError:
            return D.detect_format(path)

        # 여기가 보정 지점 — 슬라이스 비교로 OLE 를 제대로 잡는다.
        if head == D._OLE_MAGIC:
            try:
                return D._detect_ole(path)
            except Exception:
                return "ole"
        return D.detect_format(path)

    return detect


#------------------------------------------------------------------
# 추출기 생성 (보정된 감지기 주입)
#=> HybridExtractor 를 직접 조립한다. build_extractor(hybrid=True) 를 쓰면
#   기본 감지기가 박혀 나오므로 보정을 넣을 수 없다.
#
# -out: HybridExtractor 인스턴스
#------------------------------------------------------------------
def build_extractor():
    cso = _load_cso()
    from csoclassify.extract import (DocExtractor, DocxExtractor,          # noqa
                                     ExtractError, HtmlTextExtractor,
                                     Hwp5Extractor, HwpxZipExtractor,
                                     PdfiumExtractor, PlainTextExtractor,
                                     PptExtractor, PptxExtractor,
                                     SynapExeExtractor, XlsExtractor,
                                     XlsxExtractor)

    engines = {
        "pdf": PdfiumExtractor(), "hwp": Hwp5Extractor(),
        "hwpx": HwpxZipExtractor(), "doc": DocExtractor(),
        "xls": XlsExtractor(), "ppt": PptExtractor(),
        "docx": DocxExtractor(), "xlsx": XlsxExtractor(),
        "pptx": PptxExtractor(), "html": HtmlTextExtractor(),
        "text": PlainTextExtractor(),
    }
    try:
        snf = SynapExeExtractor()          # 폴백 전용
    except ExtractError:
        snf = None

    return cso.HybridExtractor(engines=engines, snf=snf,
                               detector=make_detector())


#------------------------------------------------------------------
# 표 정규화 (핵심 — R11 / R13)
#=> Word 표를 추출하면 **탭이 셀 구분자, 탭 두 개가 행 끝**으로 나온다.
#   셀 안에는 개행이 들어갈 수 있는데, 그 개행이 두 가지 뜻을 갖는다.
#
#   (A) 세로로 눕힌 여러 행  — 경조사지원규정
#       경 사 ⇥ 본인 결혼\n자녀 결혼\n… ⇥ 5\n1\n… ⇥ 500,000\n300,000\n… ⇥⇥
#       한 셀에 8줄씩 들어 있고 이는 실제로는 8개 행이다. zip 해서 펼쳐야 한다.
#
#   (B) 한 셀 안의 줄바꿈  — 출장여비규정
#       구 분 ⇥ 철도운임 ⇥ … ⇥ 일비\n(1일당) ⇥ 숙박비\n(1박당) ⇥⇥
#       "일비"와 "(1일당)"은 같은 셀이다. 이어 붙여야 한다.
#
#   🔴 이전 구현은 행 분리(⇥⇥)를 하지 않고 전체를 탭으로만 쪼갠 뒤 (A)로만
#      해석했다. 그래서 (B) 표에서 머리글이 두 행으로 쪼개지고 셀이 행을
#      넘나들며 섞였다. 그 결과 모델이 "1일당 1박당 1일당 1일당…"처럼
#      같은 단어를 반복하며 무너졌다(REPORT §20.6).
#
#   (A)와 (B)를 가르는 기준: **셀 안 줄 수가 3 이상이고 그런 셀이 2개 이상**
#   일 때만 눕힌 행으로 본다. 2줄짜리는 "이름\n(단위)" 형태가 압도적이라
#   이어 붙이는 쪽이 안전하다.
#
# -in: text = 추출 원문
#
# -out: (text, n_rows) = 정규화된 텍스트, 만들어진 표 행 수
#------------------------------------------------------------------
_MIN_STACK = 3          # 이만큼 줄이 쌓여야 '눕힌 행'으로 본다
# 한 행이 이보다 많은 행으로 펼쳐지면 표가 아니라 본문 덩어리로 본다.
# PDF 추출기는 정렬용 공백을 탭으로 뱉는 일이 있어 표처럼 보인다.
_MAX_STACK = 60


def normalize_tables(text):
    if "\t" not in text:
        return text, 0

    out, made = [], 0

    # 탭 두 개가 행 끝이다. 표가 아닌 본문에는 탭이 없으므로 안전하게 쪼갤 수 있다.
    for part in text.split("\t\t"):
        if "\t" not in part:
            out.append(part)
            continue

        cells = part.split("\t")

        # 첫 칸에는 표 앞의 본문이 붙어 나온다("… (단위 : 원)\n구 분").
        # 마지막 줄만 셀로 보고 앞부분은 본문으로 되돌린다.
        if "\n" in cells[0]:
            head, _, first = cells[0].rpartition("\n")
            if head.strip():
                out.append(head)
            cells[0] = first

        rows = _expand_row(cells)
        if not rows:
            out.append(part)
            continue
        for r in rows:
            out.append("| " + " | ".join(r) + " |")
            made += 1

    result = "\n".join(out)

    # 🔴 무손실 보장 — 표 정규화는 배치만 바꿀 뿐 내용을 지우지 않는다.
    #   초기 구현이 이 성질을 깨뜨려 PDF 한 건에서 93% 를 날렸다. 규칙으로
    #   막지 말고 **결과를 직접 확인해서** 손실이 있으면 원문을 돌려준다.
    #   공백은 배치가 바뀌며 달라지므로 비교에서 뺀다.
    if _content(result) < _content(text):
        return text, 0
    return result, made


def _content(s):
    return len(re.sub(r"\s+", "", s))


#------------------------------------------------------------------
# 행 하나를 펼치기
#=> 셀 안에 눕혀 있는 여러 행이면 zip 해서 펼치고, 아니면 한 행으로 낸다.
#
# -in: cells = 탭으로 쪼갠 셀 목록
#
# -out: [[셀, ...], ...] — 표로 볼 수 없으면 []
#------------------------------------------------------------------
def _expand_row(cells):
    cells = [c.strip("\r") for c in cells]
    if sum(1 for c in cells if c.strip()) < 2:
        return []

    counts = [len(c.split("\n")) for c in cells]
    stacked = [n for n in counts if n >= _MIN_STACK]

    # (A) 눕힌 행 — 줄이 쌓인 칸이 2개 이상이어야 한다.
    #
    #   🔴 행 수는 **가장 긴 칸**에 맞춘다. 처음에는 '가장 흔한 줄 수'에
    #      맞췄는데, 그보다 긴 칸의 나머지 줄이 통째로 버려졌다.
    #      ARIA-WISC.pdf 가 41,773자 → 2,990자(93% 손실)로 무너졌다.
    #      짧은 칸은 빈칸으로 채운다 — 이러면 원리적으로 무손실이다.
    if len(stacked) >= 2 and max(stacked) <= _MAX_STACK:
        n = max(stacked)
        rows = []
        for i in range(n):
            row = []
            for c in cells:
                lines = c.split("\n")
                # 한 줄짜리 칸은 그룹 이름표다 — 모든 행에 되풀이한다.
                row.append(_flat(lines[0]) if len(lines) == 1
                           else _flat(lines[i]) if i < len(lines) else "")
            if any(x for x in row):
                rows.append(row)
        return rows

    # (B) 한 행 — 셀 안 개행은 줄바꿈이므로 이어 붙인다.
    return [[_flat(c.replace("\n", " ")) for c in cells]]


def _flat(s):
    return " ".join(s.split())


#------------------------------------------------------------------
# Jupyter 노트북 텍스트 추출
#=> .ipynb 는 JSON 이라 통째로 넣으면 outputs/메타데이터가 노이즈가 된다.
#   마크다운 셀과 코드 셀 소스만 순서대로 뽑는다.
#------------------------------------------------------------------
def extract_ipynb(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        nb = json.load(f)

    parts = []
    for cell in nb.get("cells", []):
        src = cell.get("source", [])
        body = "".join(src) if isinstance(src, list) else str(src)
        if body.strip():
            parts.append(body)
    return "\n\n".join(parts)


#------------------------------------------------------------------
# 텍스트 정제 (핵심 — 토크나이저 방어)
#=> 추출기가 UTF-8 로 인코딩할 수 없는 문자를 뱉는 경우가 있다.
#   실제 사례: .hwp 문서에서 서로게이트 문자(0xDB80, 0xDEB1 …) 18개가 나왔다.
#   Python str 은 이를 허용하지만 tokenizers(Rust)는 유효한 UTF-8 을 요구해
#   `TypeError: TextInputSequence must be str` 로 죽는다. 인덱싱 도중 단 한
#   문서 때문에 전체가 멈추므로 추출 직후에 걸러 낸다.
#
#   서로게이트는 대부분 사용자 정의 영역(private use area) 기호라 의미가 없다.
#   되살리려 하지 않고 제거한다.
#
# -in: text
#
# -out: (text, n_changed) = 정제된 텍스트, 바뀐 문자 수
#------------------------------------------------------------------

# 유니코드 비문자(U+FFFE/U+FFFF, U+FDD0~U+FDEF)와 사용자정의영역(U+F000~U+F8FF).
# 글자 값이 없어 그대로 두면 토크나이저가 앞뒤 단어에 붙여 읽는다.
_NONCHAR = re.compile(
    "[﷐-﷯￾￿-]")


def sanitize(text):
    if not text:
        return text, 0

    dropped = 0

    # ① 유니코드 비문자·사용자정의영역 → 공백
    #   일부 PDF 는 **단어 구분자를 U+FFFE 로 뱉는다.** 실측 12건에서 발견됐고
    #   중소기업로드맵 6건에만 77,749자가 들어 있었다.
    #     "2026년￾ 10조￾ 8,588억￾ 원으로"
    #   U+FFFE 는 UTF-8 인코딩이 되기 때문에 아래 빠른 경로를 그냥 통과한다.
    #   그대로 두면 토크나이저가 단어에 붙여 읽어 BM25·임베딩이 모두 어긋난다.
    #   U+F000~U+F8FF(사용자정의영역)도 PDF 심볼 글꼴 잔재라 글자 값이 없다.
    if _NONCHAR.search(text):
        text, n = _NONCHAR.subn(" ", text)
        dropped += n

    # ② 빠른 경로: 인코딩이 되면 더 손댈 것이 없다(대부분의 문서).
    try:
        text.encode("utf-8")
        return text, dropped
    except UnicodeEncodeError:
        pass

    # ③ 짝 없는 서로게이트 — 토크나이저가 여기서 죽는다(설계서 R10).
    cleaned = "".join(c for c in text if not 0xD800 <= ord(c) <= 0xDFFF)
    return cleaned, dropped + len(text) - len(cleaned)


#------------------------------------------------------------------
# 파일 1건 텍스트 추출 (포맷별 분기 + 정제 + 표 정규화)
#
# -in: ext_obj = build_extractor() 결과, path
# -in: fix_tables = 표 정규화 적용 여부
#
# -out: (text, n_table_rows)
#------------------------------------------------------------------
def extract_text(ext_obj, path, fix_tables=True):
    if os.path.splitext(path)[1].lower() == ".ipynb":
        text, _ = sanitize(extract_ipynb(path))
        return text, 0

    text, _ = sanitize(ext_obj.extract(path))
    if not text or not fix_tables:
        return text, 0
    return normalize_tables(text)
