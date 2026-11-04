#------------------------------------------------------------------
# 문서 메타데이터 실측 — 어떤 속성을 검색 핵심 필드로 올릴지 정하는 준비 단계
#=> 분류함의 모든 파일에서 포맷별로 메타데이터(문서 속성 + 파일 시스템 정보)를
#   원본 값 그대로 뽑아 JSONL 로 저장한다. 정규화는 하지 않는다 — 실제로 어떤
#   값이 얼마나 채워져 있고 얼마나 지저분한지를 그대로 봐야 규칙을 정할 수 있다.
#
#   포맷 그룹:
#     ooxml = docx/xlsx/pptx(+템플릿)   ole = doc/xls/ppt
#     hwp   = HWP 5.0                     hwpx = HWPX
#     pdf, image, text(내부 속성 없음), other
#
#   사용:
#     python bench/meta_survey.py
#     python bench/meta_survey.py --limit 50          # 시험 삼아 50건만
#     python bench/meta_survey.py --base "D:\다른폴더"
#------------------------------------------------------------------

import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, ".."))
OUT_DIR = os.path.join(ROOT, "results", "meta_survey")
BASE = r"D:\분류함"

GROUPS = {
    "ooxml": {"docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm",
              "pptx", "pptm", "potx", "potm"},
    "ole": {"doc", "dot", "xls", "xlt", "ppt", "pot"},
    "hwp": {"hwp"},
    "hwpx": {"hwpx"},
    "pdf": {"pdf"},
    "image": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff"},
    "text": {"txt", "md", "csv", "tsv", "json", "jsonl", "html", "htm", "xml", "log"},
}

# XML 네임스페이스 — OOXML core/app 속성 파일에서 태그 이름만 뽑기 위해 사용
NS_RE = re.compile(r"^\{[^}]*\}")


#------------------------------------------------------------------
# 확장자 → 포맷 그룹 이름
#=> 파일 확장자를 보고 어떤 추출기를 쓸지 정한다.
#
# -in: ext = 소문자 확장자 (점 없이, 예: "docx")
#
# -out: group = 그룹 이름 (해당 없으면 "other")
# -out: error = 없음
#------------------------------------------------------------------
def group_of(ext):
    for g, exts in GROUPS.items():
        if ext in exts:
            return g
    return "other"


#------------------------------------------------------------------
# 값 → JSON 저장 가능한 형태
#=> datetime·bytes 등 JSON 이 모르는 타입을 문자열로 바꾼다.
#    1) datetime 은 ISO 문자열 (시간대 없으면 그대로 두어 원본 상태를 보존)
#    2) bytes 는 UTF-8 → CP949 순으로 해독 시도 (구형 Office 한글은 CP949)
#
# -in: v = 아무 값
#
# -out: v = JSON 직렬화 가능한 값
# -out: error = 없음 (해독 실패 시 replace 로 강제 변환)
#------------------------------------------------------------------
def jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bytes):
        # 구형 Office 는 UTF-8 이 아니라 시스템 코드페이지(한글=CP949)로 저장한다
        for enc in ("utf-8", "cp949"):
            try:
                return v.decode(enc).rstrip("\x00")
            except UnicodeDecodeError:
                continue
        return v.decode("utf-8", "replace").rstrip("\x00")
    if isinstance(v, str):
        return v.rstrip("\x00")
    return v


#------------------------------------------------------------------
# 파일 시스템 정보 수집
#=> 모든 포맷에 공통으로 붙는 정보(크기·날짜·해시·다운로드 출처)를 모은다.
#    1) 생성일은 Windows 의 st_birthtime (복사하면 바뀌는 값)
#    2) SHA1 해시로 중복 문서를 나중에 묶는다
#    3) NTFS 대체 데이터 스트림 Zone.Identifier 에서 다운로드 출처를 읽는다
#
# -in: path = 파일 절대경로
#
# -out: info = {size, fs_created, fs_modified, sha1, zone}
# -out: error = 없음 (해시/Zone 실패는 None 으로 기록)
#------------------------------------------------------------------
def fs_info(path):
    st = os.stat(path)
    born = getattr(st, "st_birthtime", st.st_ctime)
    info = {
        "size": st.st_size,
        "fs_created": datetime.fromtimestamp(born, timezone.utc).isoformat(),
        "fs_modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "sha1": None,
        "zone": None,
    }
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            # 큰 파일도 메모리에 다 올리지 않도록 1MB 씩 읽는다
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        info["sha1"] = h.hexdigest()
    except OSError:
        pass
    try:
        # 인터넷/메일에서 받은 파일에만 Windows 가 붙여 두는 숨은 스트림
        with open(path + ":Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            z = {}
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    z[k] = v
            info["zone"] = z
    except OSError:
        pass
    return info


#------------------------------------------------------------------
# XML 에서 태그 이름 → 텍스트 사전 만들기
#=> core.xml / app.xml 처럼 "한 단계 아래 자식 = 속성 하나" 구조인 파일을
#   {태그이름: 텍스트} 로 평평하게 편다. 네임스페이스 접두는 버린다.
#
# -in: data = XML 바이트
#
# -out: props = {태그이름: 텍스트}
# -out: error = XML 이 깨졌으면 ParseError 전파
#------------------------------------------------------------------
def flat_xml(data):
    root = ET.fromstring(data)
    props = {}
    for child in root:
        tag = NS_RE.sub("", child.tag)
        text = (child.text or "").strip()
        if text:
            props[tag] = text
    return props


#------------------------------------------------------------------
# OOXML(docx/xlsx/pptx) 메타데이터 추출
#=> ZIP 안의 속성 파일과 본문 구조에서 검색에 쓸 만한 값을 모은다.
#    1) docProps/core.xml — 제목·작성자·날짜 등 기본 속성
#    2) docProps/app.xml  — 회사·템플릿·쪽수·편집시간 등
#    3) docProps/custom.xml — 사용자 정의 속성 이름(보안등급·문서번호 등)
#    4) 포맷별 구조: Word 제목 스타일·메모/추적 작성자, Excel 시트, PPT 슬라이드 제목
#
# -in: path = 파일 절대경로
#
# -out: meta = {core.*, app.*, custom_props, ...} 사전
# -out: error = ZIP 이 아니거나 암호화된 파일이면 BadZipFile 등 예외 전파
#------------------------------------------------------------------
def ooxml_meta(path):
    meta = {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "docProps/core.xml" in names:
            for k, v in flat_xml(z.read("docProps/core.xml")).items():
                meta["core." + k] = v
        if "docProps/app.xml" in names:
            for k, v in flat_xml(z.read("docProps/app.xml")).items():
                meta["app." + k] = v
        if "docProps/custom.xml" in names:
            # 속성 값보다 "어떤 이름의 속성이 있는지"가 관심사라 이름만 모은다
            meta["custom_props"] = re.findall(
                r'name="([^"]+)"', z.read("docProps/custom.xml").decode("utf-8", "replace"))

        if "word/document.xml" in names:
            doc = z.read("word/document.xml").decode("utf-8", "replace")
            # 한글 Word 는 제목 스타일 ID 가 "1","2" 처럼 숫자라서 styles.xml 의 이름으로 판별
            heading_ids = set()
            if "word/styles.xml" in names:
                sty = z.read("word/styles.xml").decode("utf-8", "replace")
                for sid, sname in re.findall(
                        r'<w:style\b[^>]*w:styleId="([^"]+)"[^>]*>.*?<w:name w:val="([^"]+)"',
                        sty, re.S):
                    if sname.lower().startswith("heading") or sname.lower() == "title":
                        heading_ids.add(sid)
            styles_used = re.findall(r'<w:pStyle w:val="([^"]+)"', doc)
            meta["word.headings"] = sum(1 for s in styles_used if s in heading_ids)
            meta["word.track_authors"] = sorted(set(
                re.findall(r'<w:(?:ins|del)\b[^>]*w:author="([^"]*)"', doc)))
            if "word/comments.xml" in names:
                com = z.read("word/comments.xml").decode("utf-8", "replace")
                meta["word.comment_authors"] = sorted(set(re.findall(r'w:author="([^"]*)"', com)))
            # 머리글·바닥글에 문서번호/보안문구가 자주 들어가므로 텍스트 앞부분만 남긴다
            hf = []
            for n in sorted(names):
                if re.match(r"word/(header|footer)\d*\.xml$", n):
                    t = " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>",
                                            z.read(n).decode("utf-8", "replace"))).strip()
                    if t:
                        hf.append(t[:80])
            meta["word.header_footer"] = hf

        if "xl/workbook.xml" in names:
            wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
            sheets = re.findall(r"<sheet\b([^>]*)/?>", wb)
            meta["xl.sheets"] = [jsonable(re.search(r'name="([^"]*)"', s).group(1))
                                 for s in sheets if 'name="' in s]
            meta["xl.hidden_sheets"] = sum(1 for s in sheets if 'state="hidden"' in s
                                           or 'state="veryHidden"' in s)
            meta["xl.defined_names"] = len(re.findall(r"<definedName\b", wb))
            meta["xl.tables"] = sum(1 for n in names if n.startswith("xl/tables/"))
            meta["xl.ext_links"] = sum(1 for n in names if n.startswith("xl/externalLinks/")
                                       and n.endswith(".xml"))

        slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        if slides:
            titles = []
            for n in slides:
                x = z.read(n).decode("utf-8", "replace")
                # 제목 자리표시자(title/ctrTitle)가 있는 도형의 텍스트만 슬라이드 제목으로 본다
                t = ""
                for sp in re.findall(r"<p:sp>.*?</p:sp>", x, re.S):
                    if re.search(r'<p:ph[^>]*type="(?:title|ctrTitle)"', sp):
                        t = "".join(re.findall(r"<a:t>([^<]*)</a:t>", sp)).strip()
                        break
                titles.append(t)
            meta["ppt.slide_count"] = len(slides)
            meta["ppt.slide_titles"] = titles
            meta["ppt.notes"] = sum(1 for n in names if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n))
    return meta


# OLE SummaryInformation 의 속성 ID → 이름 (HWP 요약정보도 같은 ID 체계를 쓴다)
PIDSI = {2: "title", 3: "subject", 4: "author", 5: "keywords", 6: "comments",
         7: "template", 8: "last_saved_by", 9: "revision", 10: "edit_time",
         11: "last_printed", 12: "created", 13: "last_saved", 14: "pages",
         15: "words", 16: "chars", 18: "app", 19: "security", 20: "hwp_date_str",
         21: "hwp_21"}


#------------------------------------------------------------------
# 구형 Office(doc/xls/ppt) 메타데이터 추출
#=> OLE 복합문서의 SummaryInformation / DocumentSummaryInformation 을 읽는다.
#   olefile 의 get_metadata 가 두 스트림을 모두 해석해 준다.
#
# -in: path = 파일 절대경로
#
# -out: meta = {title, author, created, ..., company, category, ...}
# -out: error = OLE 파일이 아니면 olefile 예외 전파
#------------------------------------------------------------------
def ole_meta(path):
    import olefile
    meta = {}
    with olefile.OleFileIO(path) as ole:
        m = ole.get_metadata()
        for attr in ("title", "subject", "author", "keywords", "comments", "template",
                     "last_saved_by", "revision_number", "total_edit_time", "last_printed",
                     "create_time", "last_saved_time", "num_pages", "num_words",
                     "creating_application", "security", "codepage",
                     "category", "manager", "company", "slides", "notes", "hidden_slides",
                     "codepage_doc"):
            v = getattr(m, attr, None)
            if v not in (None, b"", ""):
                meta[attr] = jsonable(v)
        # 구형 Office 암호 문서는 별도 스트림 이름으로 흔적이 남는다
        meta["encrypted_hint"] = ole.exists("EncryptionInfo") or ole.exists("encryption")
    return meta


#------------------------------------------------------------------
# HWP 5.0 메타데이터 추출
#=> HWP 는 OLE 복합문서라 구조는 구형 Office 와 같지만 요약정보 스트림 이름이
#   "\x05HwpSummaryInformation" 으로 다르다. FileHeader 에서 암호/배포용 여부도 읽는다.
#    1) FileHeader 36~39 바이트 = 속성 비트 (bit0 압축, bit1 암호, bit2 배포용)
#    2) 요약정보 속성을 PIDSI 이름으로 바꿔 담는다 (모르는 ID 는 id_N 로 보존)
#
# -in: path = 파일 절대경로
#
# -out: meta = {title, author, created, ..., encrypted, distribution}
# -out: error = OLE 파일이 아니면 olefile 예외 전파
#------------------------------------------------------------------
def hwp_meta(path):
    import olefile
    meta = {}
    with olefile.OleFileIO(path) as ole:
        if ole.exists("FileHeader"):
            hdr = ole.openstream("FileHeader").read()
            if len(hdr) >= 40:
                flags = int.from_bytes(hdr[36:40], "little")
                meta["compressed"] = bool(flags & 1)
                meta["encrypted"] = bool(flags & 2)
                meta["distribution"] = bool(flags & 4)
        name = "\x05HwpSummaryInformation"
        if ole.exists(name):
            props = ole.getproperties(name, convert_time=True)
            for pid, v in props.items():
                v = jsonable(v)
                # HWP 문자열은 olefile 이 길이를 잘못 읽어 뒤 속성까지 붙어 나오므로 첫 NUL 에서 자른다
                if isinstance(v, str):
                    v = v.split("\x00", 1)[0].strip()
                if v in (None, ""):
                    continue
                meta[PIDSI.get(pid, f"id_{pid}")] = v
    return meta


#------------------------------------------------------------------
# HWPX 메타데이터 추출
#=> HWPX 는 ZIP 안 Contents/content.hpf(OPF 형식)의 <opf:metadata> 에 속성을 둔다.
#   <opf:title> 과 <opf:meta name="creator"> 같은 항목을 모두 {이름: 값} 으로 모은다.
#
# -in: path = 파일 절대경로
#
# -out: meta = {title, creator, CreatedDate, ...} (원본 이름 그대로)
# -out: error = ZIP 이 아니면 BadZipFile 전파
#------------------------------------------------------------------
def hwpx_meta(path):
    meta = {}
    with zipfile.ZipFile(path) as z:
        if "Contents/content.hpf" not in z.namelist():
            return meta
        root = ET.fromstring(z.read("Contents/content.hpf"))
        for el in root.iter():
            tag = NS_RE.sub("", el.tag)
            text = (el.text or "").strip()
            if tag == "meta" and el.get("name") and text:
                meta[el.get("name")] = text
            elif tag in ("title", "language") and text:
                meta[tag] = text
    return meta


#------------------------------------------------------------------
# PDF 메타데이터 추출
#=> pypdfium2 로 Info 사전·쪽수·양식 필드·책갈피·텍스트층 유무를 본다.
#    1) Info 사전 (Title/Author/Creator/Producer/CreationDate…)
#    2) 앞 3쪽 글자 수 — 0 에 가까우면 스캔본(OCR 필요)
#    3) XMP 블록 존재 여부는 파일 앞뒤 바이트에서 문자열로 확인 (값까지는 안 봄)
#
# -in: path = 파일 절대경로
#
# -out: meta = {Title, Author, ..., pages, form_type, toc, text_chars, xmp}
# -out: error = 암호/손상 PDF 면 pdfium 예외 전파
#------------------------------------------------------------------
def pdf_meta(path):
    import pypdfium2 as pdfium
    meta = {}
    pdf = pdfium.PdfDocument(path)
    try:
        for k, v in pdf.get_metadata_dict(skip_empty=True).items():
            meta[k] = v
        meta["pages"] = len(pdf)
        meta["form_type"] = int(pdf.get_formtype())
        meta["toc"] = sum(1 for _ in pdf.get_toc(max_depth=4))
        chars = 0
        for i in range(min(3, len(pdf))):
            page = pdf[i]
            tp = page.get_textpage()
            # 공백만 있는 텍스트층은 스캔본과 다를 바 없으니 공백을 빼고 센다
            chars += len(re.sub(r"\s", "", tp.get_text_range()))
            tp.close()
            page.close()
        meta["text_chars"] = chars
    finally:
        pdf.close()
    # XMP 는 보통 파일 앞부분에 있지만 증분 저장이면 끝에 붙으므로 양쪽 2MB 를 본다
    with open(path, "rb") as f:
        head = f.read(2 << 20)
        f.seek(max(0, os.path.getsize(path) - (2 << 20)))
        tail = f.read()
    meta["xmp"] = b"<x:xmpmeta" in head or b"<x:xmpmeta" in tail
    return meta


#------------------------------------------------------------------
# 이미지 EXIF / PNG 텍스트 청크 추출
#=> 사진·스캔 이미지의 촬영일, 기기, 작성자, GPS 존재 여부를 본다.
#   GPS 는 개인정보라 좌표는 저장하지 않고 "있음/없음"만 남긴다.
#
# -in: path = 파일 절대경로
#
# -out: meta = {width, height, DateTime, DateTimeOriginal, Make, Model, Artist, gps, png_keys}
# -out: error = 이미지가 깨졌으면 PIL 예외 전파
#------------------------------------------------------------------
def image_meta(path):
    from PIL import Image
    meta = {}
    with Image.open(path) as im:
        meta["width"], meta["height"] = im.size
        exif = im.getexif()
        for tag, name in ((306, "DateTime"), (315, "Artist"), (271, "Make"),
                          (272, "Model"), (305, "Software"), (270, "ImageDescription")):
            if exif.get(tag):
                meta[name] = jsonable(exif.get(tag))
        sub = exif.get_ifd(0x8769)
        if sub.get(36867):
            meta["DateTimeOriginal"] = jsonable(sub.get(36867))
        meta["gps"] = bool(exif.get_ifd(0x8825))
        # PNG 는 EXIF 대신 tEXt 청크(Software, Creation Time 등)를 쓴다
        meta["png_keys"] = [k for k in im.info.keys() if isinstance(k, str)
                            and k not in ("dpi", "gamma", "transparency", "aspect", "icc_profile",
                                          "exif", "srgb", "chromaticity")]
    return meta


EXTRACTORS = {"ooxml": ooxml_meta, "ole": ole_meta, "hwp": hwp_meta, "hwpx": hwpx_meta,
              "pdf": pdf_meta, "image": image_meta}


#------------------------------------------------------------------
# 파일 하나 실측
#=> 파일 시스템 정보 + 포맷별 메타데이터를 한 레코드로 묶는다.
#   추출 실패는 레코드에 오류 문자열로 남겨 통계에 포함시킨다 (실패율도 실측 대상).
#
# -in: path = 파일 절대경로
# -in: base = 기준 폴더 (상대경로·최상위 분류 계산용)
#
# -out: rec = {rel, top, ext, group, ...fs_info, meta, error}
# -out: error = 없음 (추출 예외는 rec["error"] 에 기록)
#------------------------------------------------------------------
def survey_file(path, base):
    rel = os.path.relpath(path, base)
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    g = group_of(ext)
    rec = {"rel": rel, "top": rel.split(os.sep)[0] if os.sep in rel else "",
           "depth": rel.count(os.sep), "name": os.path.basename(path),
           "ext": ext, "group": g, "meta": {}, "error": None}
    rec.update(fs_info(path))
    # 확장자만 믿지 않는다 — .doc 인데 실제로는 docx(ZIP) 인 파일이 실측에서 나왔다
    if g == "ole":
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic == b"PK\x03\x04":
            g = rec["group"] = "ooxml"
            rec["ext_mismatch"] = True
        elif not magic:
            rec["error"] = "EmptyFile: 0 bytes"
            return rec
    fn = EXTRACTORS.get(g)
    if fn:
        try:
            rec["meta"] = {k: jsonable(v) for k, v in fn(path).items()}
        except Exception as e:  # noqa: BLE001 — 실패 유형 자체를 집계하려고 모두 잡는다
            rec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return rec


#------------------------------------------------------------------
# 진입점 — 폴더 전체를 돌며 JSONL 로 저장
#=> 기준 폴더 아래 모든 파일을 실측해 results/meta_survey/records.jsonl 에 쓴다.
#
# -in: 없음 (명령행 --base, --limit, --out)
#
# -out: 없음 (파일 저장, 진행 상황 출력)
# -out: error = 개별 파일 오류는 기록 후 계속, 폴더 접근 불가 시 OSError 전파
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    paths = []
    for d, _, files in os.walk(args.base):
        for f in files:
            paths.append(os.path.join(d, f))
    paths.sort()
    if args.limit:
        paths = paths[:args.limit]

    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "records.jsonl")
    t0 = time.time()
    with open(out, "w", encoding="utf-8") as f:
        for i, p in enumerate(paths, 1):
            f.write(json.dumps(survey_file(p, args.base), ensure_ascii=False) + "\n")
            if i % 200 == 0:
                print(f"  {i}/{len(paths)}  {time.time() - t0:.0f}s", flush=True)
    print(f"완료: {len(paths)}건, {time.time() - t0:.1f}s → {out}")


if __name__ == "__main__":
    main()
