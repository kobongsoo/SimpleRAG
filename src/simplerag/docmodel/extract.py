#------------------------------------------------------------------
# 구조화 추출 진입점 (재설계서 §5 · §13)
#=> 파일 하나를 Document(블록 목록)로 만든다.
#    1) 확장자와 파일 머리(매직 바이트)로 포맷을 고른다
#    2) 구조 파서가 있는 포맷(docx·pptx·xlsx·hwpx·html·md·ipynb + Phase 2: doc·pdf)은 파서 → 후처리
#    3) 파서가 없거나 실패하면 기존 추출기(legacy)의 평문을 raw_text 로 담는다
#       — 그 경로는 청킹도 기존 split_structured 를 그대로 타서 지금과 같은 청크를 낸다
#   한 문서의 실패가 인덱싱 전체를 멈추지 않도록, 파서 예외는 모두 legacy 폴백으로 바꾼다.
#------------------------------------------------------------------

import json
import os

from . import blocks as B
from . import postprocess

# 파일 머리 매직 — docx/pptx/xlsx/hwpx 는 ZIP, doc 은 OLE(복합 문서), pdf 는 %PDF
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_MAGIC = b"%PDF"


#------------------------------------------------------------------
# 확장자 → 파서 함수
#=> 함수는 필요할 때 가져온다(포맷 하나 쓰려고 모든 파서 모듈을 적재하지 않게).
#
# -in: ext = 소문자 확장자(".docx" 등)
#
# -out: (파서 함수, 파일 머리 매직 또는 None) 또는 (None, None)
# -out: error = 없음
#------------------------------------------------------------------
def _parser_for(ext):
    if ext == ".docx":
        from .ooxml_docx import extract
        return extract, _ZIP_MAGIC
    if ext == ".pptx":
        from .ooxml_pptx import extract
        return extract, _ZIP_MAGIC
    if ext in (".xlsx", ".xlsm"):
        from .ooxml_xlsx import extract
        return extract, _ZIP_MAGIC
    if ext == ".hwpx":
        from .hwpx import extract
        return extract, _ZIP_MAGIC
    if ext == ".doc":
        from .doc_bin import extract
        return extract, _OLE_MAGIC
    if ext == ".pdf":
        from .pdf import extract
        return extract, _PDF_MAGIC
    if ext in (".html", ".htm"):
        from .html_md import extract_html
        return extract_html, None
    if ext in (".md", ".markdown"):
        from .html_md import extract_md
        return extract_md, None
    if ext == ".ipynb":
        from .html_md import extract_ipynb
        return extract_ipynb, None
    return None, None


#------------------------------------------------------------------
# 블록 경로로 처리하는 확장자인가
#=> 진단·통계에서 "이 문서는 어느 경로를 탔나"를 미리 알 때 쓴다.
#
# -in: path = 파일 경로
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def supports(path):
    return _parser_for(os.path.splitext(path)[1].lower())[0] is not None


#------------------------------------------------------------------
# 파일 머리가 기대한 포맷인가
#=> 확장자만 .docx 이고 실제로는 옛 .doc(OLE)인 파일, 확장자만 .doc 인 RTF·HTML 등이 있다.
#   그런 파일을 엉뚱한 파서에 넣으면 실패하므로 미리 가려 legacy 로 보낸다.
#
# -in: path  = 파일 경로
# -in: magic = 기대하는 머리 바이트
#
# -out: bool
# -out: error = 없음 (읽기 실패면 False)
#------------------------------------------------------------------
def _has_magic(path, magic):
    try:
        with open(path, "rb") as f:
            return f.read(len(magic)) == magic
    except OSError:
        return False


#------------------------------------------------------------------
# 문서 하나 추출 (핵심)
#
# -in: path   = 파일 경로
# -in: legacy = 기존 추출 함수 fn(path) → (text, n_rows). 파서가 없거나 실패할 때 쓴다
# -in: title  = 문서 제목(None 이면 파일명에서 확장자를 뗀 것)
#
# -out: Document — 블록 경로(is_blocks) 또는 폴백 평문(raw_text)
# -out: error = legacy 도 실패하면 그 예외를 전파(상위 indexer 가 실패로 기록)
#------------------------------------------------------------------
def extract_document(path, legacy=None, title=None):
    if title is None:
        title = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    parser, magic = _parser_for(ext)
    warnings = []

    if parser is not None and (magic is None or _has_magic(path, magic)):
        try:
            doc = parser(path, title)
            postprocess.finalize(doc)
            return doc
        except Exception as e:     # noqa: BLE001 — 파서 실패는 legacy 폴백 신호
            warnings.append("구조 파서 실패 → 기존 추출: {}: {}".format(
                type(e).__name__, str(e)[:120]))
    elif parser is not None:
        warnings.append("확장자는 {} 이지만 파일 머리가 다름 → 기존 추출".format(ext))

    if legacy is None:
        raise ValueError("구조 파서가 없는 포맷이고 기존 추출기도 주어지지 않음: " + ext)
    text, _ = legacy(path)
    return B.Document(format="legacy", title=title, raw_text=text or "", warnings=warnings)


#------------------------------------------------------------------
# 진단용 블록 JSON 저장
#=> Newploy 의 document_result.json 에 해당한다. 청크가 이상할 때 "추출이 블록을 어떻게
#   나눴나"를 눈으로 확인하는 용도다.
#
# -in: doc     = Document
# -in: out_dir = 저장 폴더(없으면 만든다)
# -in: name    = 파일 이름 바탕(보통 원본 파일명)
#
# -out: str = 저장한 경로
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def dump_json(doc, out_dir, name):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".blocks.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc.to_dict(), f, ensure_ascii=False, indent=1)
    return path
