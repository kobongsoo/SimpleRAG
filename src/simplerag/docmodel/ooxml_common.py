#------------------------------------------------------------------
# OOXML(docx·pptx·xlsx) 공통 도우미
#=> 세 포맷 모두 ZIP 안의 XML 이고 네임스페이스가 붙은 태그를 쓴다. 표준 라이브러리
#   (zipfile + ElementTree)만 쓴다 — python-docx 등을 넣지 않아 exe 크기를 지킨다.
#   이식 출처: MpowerClassify csoclassify/extract/office_ooxml.py (_local, _num_key)
#------------------------------------------------------------------

import posixpath
import re
from xml.etree import ElementTree as ET

# 관계(rels) 파일의 태그 이름 — 네임스페이스가 바뀌어도 지역명은 같다
_REL = "Relationship"


#------------------------------------------------------------------
# 태그의 지역명
#=> '{긴URL}t' 처럼 붙는 네임스페이스를 떼고 't' 만 남긴다. 네임스페이스 버전이
#   달라도 같은 코드로 읽기 위해서다.
#
# -in: tag = ElementTree 요소의 tag 문자열
#
# -out: str = 지역 태그명
# -out: error = 없음
#------------------------------------------------------------------
def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else tag


#------------------------------------------------------------------
# 속성값 읽기 (네임스페이스 무시)
#=> w:val, r:id 처럼 속성에도 네임스페이스가 붙는다. 지역명이 같은 첫 속성을 돌려준다.
#
# -in: el   = 요소
# -in: name = 속성 지역명(예: "val")
#
# -out: str 또는 None
# -out: error = 없음
#------------------------------------------------------------------
def attr(el, name):
    if el is None:
        return None
    for k, v in el.attrib.items():
        if local(k) == name:
            return v
    return None


#------------------------------------------------------------------
# 자식 중 지역명이 같은 첫 요소
#
# -in: el   = 부모 요소
# -in: name = 찾을 지역명
#
# -out: 요소 또는 None
# -out: error = 없음
#------------------------------------------------------------------
def child(el, name):
    if el is None:
        return None
    for ch in el:
        if local(ch.tag) == name:
            return ch
    return None


#------------------------------------------------------------------
# 자손 중 지역명이 같은 모든 요소(문서 순서)
#
# -in: el   = 시작 요소
# -in: name = 찾을 지역명
#
# -out: list[요소]
# -out: error = 없음
#------------------------------------------------------------------
def descendants(el, name):
    return [e for e in el.iter() if local(e.tag) == name]


#------------------------------------------------------------------
# 파일 이름 속 번호로 정렬하기 위한 키
#=> "slide10.xml" 이 "slide2.xml" 보다 뒤에 오게 한다.
#
# -in: name = zip 항목 이름
# -in: rx   = 번호를 잡는 정규식(그룹 1 이 번호)
#
# -out: int = 번호(못 찾으면 0)
# -out: error = 없음
#------------------------------------------------------------------
def num_key(name, rx):
    m = rx.search(name)
    return int(m.group(1)) if m else 0


#------------------------------------------------------------------
# XML 파싱 (zip 항목)
#
# -in: z    = 열린 ZipFile
# -in: name = 항목 이름
#
# -out: 루트 요소 또는 None(항목 없음·손상)
# -out: error = 없음 (부속 파일 하나 때문에 문서 전체를 버리지 않는다)
#------------------------------------------------------------------
def parse(z, name):
    try:
        return ET.fromstring(z.read(name))
    except (KeyError, ET.ParseError, OSError):
        return None


#------------------------------------------------------------------
# 관계 파일 읽기 — rId → 대상 경로
#=> 슬라이드 → 노트, 통합문서 → 시트처럼 파일끼리의 짝은 번호가 아니라 rels 로
#   맺어져 있다. 번호만 보고 짝지으면 어긋날 수 있어 rels 를 따른다.
#
# -in: z        = 열린 ZipFile
# -in: part     = 관계를 볼 부품 경로(예: "ppt/slides/slide1.xml")
#
# -out: dict = {rId: (Type 끝 이름, zip 안 절대 경로)}
# -out: error = 없음 (rels 가 없으면 빈 사전)
#------------------------------------------------------------------
def rels(z, part):
    folder, base = posixpath.split(part)
    root = parse(z, posixpath.join(folder, "_rels", base + ".rels"))
    out = {}
    if root is None:
        return out
    for r in root:
        if local(r.tag) != _REL:
            continue
        target = r.get("Target") or ""
        if r.get("TargetMode") == "External":
            continue
        # 대상은 부품 기준 상대 경로다("../notesSlides/notesSlide1.xml")
        full = posixpath.normpath(posixpath.join(folder, target)) if not target.startswith("/") \
            else target.lstrip("/")
        out[r.get("Id")] = ((r.get("Type") or "").rsplit("/", 1)[-1], full)
    return out


# 셀 주소("AB12") → (행, 열) 0부터
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


#------------------------------------------------------------------
# 엑셀 셀 주소 해석
#
# -in: ref = "B3" 같은 주소
#
# -out: (행, 열) 0부터 또는 None(해석 불가)
# -out: error = 없음
#------------------------------------------------------------------
def cell_ref(ref):
    m = _CELL_REF.fullmatch((ref or "").upper())
    if not m:
        return None
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)) - 1, col - 1
