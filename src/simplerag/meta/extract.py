#------------------------------------------------------------------
# 메타데이터 원본 값 추출 (메타데이터 검색 설계서 §6 · §10-1 · §15)
#=> 파일 하나에서 문서 속성과 파일 시스템 정보를 "원본 값 그대로" 뽑는다(RawMeta).
#   정규화는 normalize.py 한곳에서 한다 — 그래야 규칙이 바뀌어도 파일을 다시 읽지 않고
#   raw 로 다시 계산할 수 있다(meta rebuild).
#   bench/meta_survey.py(실측 도구)의 추출기를 정식화한 것이고, 수집하지 않기로 한 항목
#   (§5 · §15: 메모 · 변경 추적 작성자, 사용자 정의 속성, 키워드 · 주제 · 회사, HWP 설명란의
#   DRM 추적 정보, GPS, 다운로드 전체 URL)은 여기서 아예 읽지 않는다.
#------------------------------------------------------------------

import hashlib
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

NS_RE = re.compile(r"^\{[^}]*\}")

# 확장자 → 형식(§4 #3). 매직 바이트로 못 정할 때만 쓴다
EXT_FORMAT = {
    "word": {"doc", "docx", "docm", "dot", "dotx", "dotm", "rtf"},
    "excel": {"xls", "xlsx", "xlsm", "xlt", "xltx", "xltm", "csv"},
    "ppt": {"ppt", "pptx", "pptm", "pot", "potx", "potm"},
    "hwp": {"hwp", "hwpx", "hwt"},
    "pdf": {"pdf"},
    "image": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp"},
    "text": {"txt", "md", "tsv", "json", "jsonl", "html", "htm", "xml", "log", "py", "ipynb"},
}


#------------------------------------------------------------------
# 확장자 → 형식 이름
#
# -in: ext = 소문자 확장자(점 없이)
#
# -out: format = word|excel|ppt|hwp|pdf|image|text|other
# -out: error = 없음
#------------------------------------------------------------------
def format_of_ext(ext):
    for f, exts in EXT_FORMAT.items():
        if ext in exts:
            return f
    return "other"


#------------------------------------------------------------------
# 값 → 문자열(바이트면 UTF-8 → CP949 로 풀기, 뒤 NUL 제거)
#
# -in: v = 아무 값
#
# -out: 문자열 · 숫자 · None (datetime 은 ISO 문자열)
# -out: error = 없음 (못 푸는 바이트는 replace)
#------------------------------------------------------------------
def _val(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bytes):
        # 구형 Office 는 시스템 코드페이지(한글 = CP949)로 저장한다
        for enc in ("utf-8", "cp949"):
            try:
                return v.decode(enc).split("\x00", 1)[0].strip()
            except UnicodeDecodeError:
                continue
        return v.decode("utf-8", "replace").split("\x00", 1)[0].strip()
    if isinstance(v, str):
        return v.split("\x00", 1)[0].strip()
    return v


#------------------------------------------------------------------
# 매직 바이트로 형식 판정 (§10-1)
#=> 확장자는 거짓말을 한다(.doc 인데 ZIP, 0바이트 등). 앞 바이트와 안쪽 구조로 정한다.
#    1) PK → [Content_Types].xml 로 docx/xlsx/pptx, mimetype 이 hwp+zip 이면 HWPX
#    2) OLE → 스트림 이름으로 doc/xls/ppt/hwp
#    3) %PDF → pdf, 0바이트 → empty
#    4) 그 밖은 확장자
#
# -in: path = 파일 경로 / ext = 소문자 확장자
#
# -out: (kind, format, status) — kind = ooxml|ole|hwp|hwpx|pdf|none(속성 없음)
# -out: error = 없음 (못 읽으면 확장자 판정)
#------------------------------------------------------------------
def detect(path, ext):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "none", format_of_ext(ext), "failed"
    if not head:
        return "none", format_of_ext(ext), "empty"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "mimetype" in names and b"hwp" in z.read("mimetype").lower():
                    return "hwpx", "hwp", "ok"
                if "[Content_Types].xml" in names:
                    ct = z.read("[Content_Types].xml").decode("utf-8", "replace")
                    for key, fmt in (("wordprocessingml", "word"), ("spreadsheetml", "excel"),
                                     ("presentationml", "ppt")):
                        if key in ct:
                            return "ooxml", fmt, "ok"
        except (zipfile.BadZipFile, OSError):
            return "none", format_of_ext(ext), "failed"
        return "none", format_of_ext(ext), "ok"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        try:
            import olefile
            with olefile.OleFileIO(path) as ole:
                if ole.exists("FileHeader"):
                    return "hwp", "hwp", "ok"
                if ole.exists("WordDocument"):
                    return "ole", "word", "ok"
                if ole.exists("Workbook") or ole.exists("Book"):
                    return "ole", "excel", "ok"
                if ole.exists("PowerPoint Document"):
                    return "ole", "ppt", "ok"
                # 암호화된 OOXML 은 OLE 껍데기에 EncryptionInfo 만 있다
                if ole.exists("EncryptionInfo"):
                    return "none", format_of_ext(ext), "encrypted"
                return "ole", format_of_ext(ext), "ok"
        except Exception:  # noqa: BLE001 — 깨진 OLE 는 확장자로
            return "none", format_of_ext(ext), "failed"
    if head.startswith(b"%PDF"):
        return "pdf", "pdf", "ok"
    return "none", format_of_ext(ext), "ok"


#------------------------------------------------------------------
# 파일 시스템 정보
#=> 모든 파일에 공통 — 크기 · 생성일(PC 에 들어온 날) · 수정일 · 내용 해시 · 받은 곳.
#   Zone.Identifier 는 도메인만 남긴다(전체 URL 의 쿼리에 토큰이 있을 수 있다, §15).
#
# -in: path = 파일 경로 / want_hash = 내용 해시를 셀지(색인이 이미 셌으면 False)
#
# -out: {size, created, modified, sha1, host, referrer_host}
# -out: error = stat 실패는 OSError 전파 (해시 · Zone 실패는 None)
#------------------------------------------------------------------
def fs_info(path, want_hash=True):
    st = os.stat(path)
    born = getattr(st, "st_birthtime", st.st_ctime)
    info = {"size": st.st_size,
            "created": datetime.fromtimestamp(born, timezone.utc).isoformat(),
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "sha1": None, "host": None, "referrer_host": None}
    if want_hash:
        try:
            h = hashlib.sha1()
            with open(path, "rb") as f:
                # 큰 파일도 메모리에 다 올리지 않게 1MB 씩
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            info["sha1"] = h.hexdigest()
        except OSError:
            pass
    try:
        # 인터넷 · 메일에서 받은 파일에만 Windows 가 붙이는 숨은 스트림(NTFS)
        with open(path + ":Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == "HostUrl" and v:
                    info["host"] = (urlparse(v).hostname or "").lower() or None
                elif k == "ReferrerUrl" and v:
                    info["referrer_host"] = (urlparse(v).hostname or "").lower() or None
    except OSError:
        pass
    return info


#------------------------------------------------------------------
# XML 한 단계 아래 자식을 {태그: 글} 로 펴기 (core.xml · app.xml)
#
# -in: data = XML 바이트
#
# -out: {태그이름: 글}
# -out: error = XML 이 깨졌으면 ParseError 전파
#------------------------------------------------------------------
def _flat_xml(data):
    out = {}
    for child in ET.fromstring(data):
        t = (child.text or "").strip()
        if t:
            out[NS_RE.sub("", child.tag)] = t
    return out


#------------------------------------------------------------------
# OOXML(docx · xlsx · pptx) 속성
#=> core.xml(제목 · 만든 이 · 저장자 · 날짜) · app.xml(템플릿 · 쪽 · 슬라이드 · 프로그램) · 구조
#   (Word 제목 스타일 수, Excel 시트명, PPT 슬라이드 제목). 키워드 · 주제 · 설명 · 회사 ·
#   사용자 정의 속성 · 메모 · 변경 추적 작성자는 읽지 않는다(§5).
#
# -in: path = 파일 경로
#
# -out: {title, author, last_saved_by, created, modified, template, pages, slides, app, sheets, slide_titles, headings}
# -out: error = ZIP 이 깨졌으면 예외 전파
#------------------------------------------------------------------
def ooxml_meta(path):
    m = {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "docProps/core.xml" in names:
            c = _flat_xml(z.read("docProps/core.xml"))
            for src, dst in (("title", "title"), ("creator", "author"), ("lastModifiedBy", "last_saved_by"),
                             ("created", "created"), ("modified", "modified")):
                if c.get(src):
                    m[dst] = c[src]
        if "docProps/app.xml" in names:
            a = _flat_xml(z.read("docProps/app.xml"))
            for src, dst in (("Template", "template"), ("Pages", "pages"), ("Slides", "slides"),
                             ("Application", "app")):
                if a.get(src):
                    m[dst] = a[src]
        if "word/document.xml" in names and "word/styles.xml" in names:
            # 한글 Word 의 제목 스타일 ID 는 "1" 처럼 숫자라 styles.xml 의 이름으로 판별
            sty = z.read("word/styles.xml").decode("utf-8", "replace")
            ids = {sid for sid, nm in re.findall(
                r'<w:style\b[^>]*w:styleId="([^"]+)"[^>]*>.*?<w:name w:val="([^"]+)"', sty, re.S)
                if nm.lower().startswith("heading") or nm.lower() == "title"}
            doc = z.read("word/document.xml").decode("utf-8", "replace")
            m["headings"] = sum(1 for s in re.findall(r'<w:pStyle w:val="([^"]+)"', doc) if s in ids)
        if "xl/workbook.xml" in names:
            wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
            m["sheets"] = [s for s in re.findall(r'<sheet\b[^>]*\bname="([^"]*)"', wb)]
        slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        if slides:
            titles = []
            for n in slides:
                x = z.read(n).decode("utf-8", "replace")
                t = ""
                # 제목 자리표시자(title · ctrTitle) 도형의 글만 슬라이드 제목으로 본다
                for sp in re.findall(r"<p:sp>.*?</p:sp>", x, re.S):
                    if re.search(r'<p:ph[^>]*type="(?:title|ctrTitle)"', sp):
                        t = "".join(re.findall(r"<a:t>([^<]*)</a:t>", sp)).strip()
                        break
                titles.append(t)
            m["slides"] = len(slides)
            m["slide_titles"] = titles
    return m


#------------------------------------------------------------------
# 구형 Office(doc · xls · ppt) 속성 — OLE SummaryInformation
#=> 제목 · 작성자 · 템플릿 · 저장자 · 생성/저장일 · 쪽 · 프로그램 · 슬라이드 수.
#   메모(comments) · 키워드 · 주제 · 회사 · 관리자 · 범주는 읽지 않는다(§5).
#
# -in: path = 파일 경로
#
# -out: {title, author, template, last_saved_by, created, modified, pages, app, slides, codepage}
# -out: error = OLE 가 깨졌으면 예외 전파
#------------------------------------------------------------------
def ole_meta(path):
    import olefile
    m = {}
    with olefile.OleFileIO(path) as ole:
        md = ole.get_metadata()
        for src, dst in (("title", "title"), ("author", "author"), ("template", "template"),
                         ("last_saved_by", "last_saved_by"), ("create_time", "created"),
                         ("last_saved_time", "modified"), ("num_pages", "pages"),
                         ("creating_application", "app"), ("slides", "slides"), ("codepage", "codepage")):
            v = _val(getattr(md, src, None))
            if v not in (None, ""):
                m[dst] = v
    return m


# HWP 요약정보 속성 ID — 쓰는 것만(6 설명란은 DRM 추적 정보가 들어 있어 읽지 않는다, §15)
HWP_PIDS = {2: "title", 4: "author", 7: "template", 8: "last_saved_by", 12: "created",
            13: "modified", 14: "pages", 20: "date_str"}


#------------------------------------------------------------------
# HWP 5.0 속성 — \005HwpSummaryInformation + FileHeader 속성 비트
#=> olefile 이 HWP 문자열 길이를 잘못 읽어 뒤 속성까지 붙여 주므로 첫 NUL 에서 자른다(실측).
#
# -in: path = 파일 경로
#
# -out: {title, author, ..., encrypted, distribution}
# -out: error = OLE 가 깨졌으면 예외 전파
#------------------------------------------------------------------
def hwp_meta(path):
    import olefile
    m = {}
    with olefile.OleFileIO(path) as ole:
        if ole.exists("FileHeader"):
            hdr = ole.openstream("FileHeader").read()
            if len(hdr) >= 40:
                flags = int.from_bytes(hdr[36:40], "little")
                m["encrypted"] = bool(flags & 2)
                m["distribution"] = bool(flags & 4)
        name = "\x05HwpSummaryInformation"
        if ole.exists(name):
            for pid, v in ole.getproperties(name, convert_time=True).items():
                key = HWP_PIDS.get(pid)
                if key is None:
                    continue
                v = _val(v)
                if v not in (None, ""):
                    m[key] = v
    return m


#------------------------------------------------------------------
# HWPX 속성 — Contents/content.hpf 의 <opf:meta>
#
# -in: path = 파일 경로
#
# -out: {title, author, last_saved_by, created, modified}
# -out: error = ZIP 이 깨졌으면 예외 전파
#------------------------------------------------------------------
def hwpx_meta(path):
    m = {}
    with zipfile.ZipFile(path) as z:
        if "Contents/content.hpf" not in z.namelist():
            return m
        pick = {"creator": "author", "lastsaveby": "last_saved_by", "CreatedDate": "created",
                "ModifiedDate": "modified"}
        for el in ET.fromstring(z.read("Contents/content.hpf")).iter():
            tag = NS_RE.sub("", el.tag)
            t = (el.text or "").strip()
            if not t:
                continue
            if tag == "meta" and el.get("name") in pick:
                m[pick[el.get("name")]] = t
            elif tag == "title":
                m["title"] = t
    return m


#------------------------------------------------------------------
# PDF 속성 — Info 사전 · 쪽 수 · 앞 3쪽 글자 수(스캔 판정) · 책갈피 수
#
# -in: path = 파일 경로
#
# -out: {title, author, creator, producer, created, modified, pages, text_chars, toc}
# -out: error = 암호 · 손상 PDF 면 pdfium 예외 전파
#------------------------------------------------------------------
def pdf_meta(path):
    import pypdfium2 as pdfium
    m = {}
    pdf = pdfium.PdfDocument(path)
    try:
        info = pdf.get_metadata_dict(skip_empty=True)
        for src, dst in (("Title", "title"), ("Author", "author"), ("Creator", "creator"),
                         ("Producer", "producer"), ("CreationDate", "created"), ("ModDate", "modified")):
            if info.get(src):
                m[dst] = _val(info[src])
        m["pages"] = len(pdf)
        m["toc"] = sum(1 for _ in pdf.get_toc(max_depth=2))
        chars = 0
        for i in range(min(3, len(pdf))):
            page = pdf[i]
            tp = page.get_textpage()
            # 공백만 있는 글자층은 스캔본과 같으니 공백을 빼고 센다
            chars += len(re.sub(r"\s", "", tp.get_text_range()))
            tp.close()
            page.close()
        m["text_chars"] = chars
    finally:
        pdf.close()
    return m


EXTRACTORS = {"ooxml": ooxml_meta, "ole": ole_meta, "hwp": hwp_meta, "hwpx": hwpx_meta, "pdf": pdf_meta}


#------------------------------------------------------------------
# 파일 하나의 원본 메타데이터 (RawMeta)
#=> 형식 판정 → 파일 시스템 정보 → 포맷별 추출. 추출 실패는 status 로 남긴다.
#   이미지 · 텍스트 · 기타는 파일 시스템 정보만(결정 D6).
#
# -in: path = 파일 경로 / sha1 = 색인이 이미 센 내용 해시(있으면 다시 세지 않음)
#
# -out: raw = {name, ext, kind, format, ext_mismatch, status, error, fs, doc}
# -out: error = 파일이 없으면 OSError 전파 (속성 추출 실패는 raw["status"] = "failed")
#------------------------------------------------------------------
def extract_raw(path, sha1=None):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    kind, fmt, status = detect(path, ext)
    fs = fs_info(path, want_hash=sha1 is None)
    if sha1:
        fs["sha1"] = sha1
    raw = {"name": name, "ext": ext, "kind": kind, "format": fmt,
           "ext_mismatch": format_of_ext(ext) != fmt and fmt != "other",
           "status": status, "error": None, "fs": fs, "doc": {}}
    fn = EXTRACTORS.get(kind)
    if fn and status == "ok":
        try:
            raw["doc"] = {k: _val(v) if not isinstance(v, list) else [_val(x) for x in v]
                          for k, v in fn(path).items()}
            if raw["doc"].get("encrypted"):
                raw["status"] = "encrypted"
        except Exception as e:  # noqa: BLE001 — 속성을 못 읽어도 파일 시스템 정보로 쓴다
            msg = str(e)[:120]
            raw["status"] = "encrypted" if "password" in msg.lower() or "encrypt" in msg.lower() else "failed"
            raw["error"] = "{}: {}".format(type(e).__name__, msg)
    return raw
