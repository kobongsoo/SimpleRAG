//------------------------------------------------------------------
// 메타데이터 원본 값 추출 (메타데이터 검색 설계서 §6 · §10-1 · §15, Python meta/extract.py)
//=> 파일 하나에서 문서 속성과 파일 시스템 정보를 "원본 값 그대로" 뽑는다(RawMeta, JSON).
//   정규화는 normalize 한곳에서 한다 — 그래야 규칙이 바뀌어도 파일을 다시 읽지 않고
//   raw 로 다시 계산할 수 있다(meta rebuild).
//   raw 의 키 이름 · 값 모양(문자열 · 정수 · 날짜 isoformat)은 Python 판과 같아야 한다 —
//   두 판이 같은 meta.sqlite 를 함께 쓴다.
//   수집하지 않기로 한 항목(§5 · §15: 메모 · 변경 추적 작성자, 사용자 정의 속성, 키워드 · 주제 ·
//   회사, HWP 설명란(PID 6)의 DRM 추적 정보, GPS, 다운로드 전체 URL)은 아예 읽지 않는다.
//------------------------------------------------------------------

use std::io::Read;

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};

use super::pyfmt;
use crate::pyutil;
use crate::xmldom;

// 확장자 → 형식(§4 #3). 매직 바이트로 못 정할 때만 쓴다(파이썬 dict 순서대로 찾는다)
const EXT_FORMAT: &[(&str, &[&str])] = &[
    ("word", &["doc", "docx", "docm", "dot", "dotx", "dotm", "rtf"]),
    ("excel", &["xls", "xlsx", "xlsm", "xlt", "xltx", "xltm", "csv"]),
    ("ppt", &["ppt", "pptx", "pptm", "pot", "potx", "potm"]),
    ("hwp", &["hwp", "hwpx", "hwt"]),
    ("pdf", &["pdf"]),
    ("image", &["jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp"]),
    ("text", &["txt", "md", "tsv", "json", "jsonl", "html", "htm", "xml", "log", "py", "ipynb"]),
];

//------------------------------------------------------------------
// 확장자 → 형식 이름
//
// -in: ext = 소문자 확장자(점 없이)
//
// -out: word|excel|ppt|hwp|pdf|image|text|other
// -out: error = 없음
//------------------------------------------------------------------
pub fn format_of_ext(ext: &str) -> &'static str {
    for (f, exts) in EXT_FORMAT {
        if exts.contains(&ext) {
            return f;
        }
    }
    "other"
}

//------------------------------------------------------------------
// 포맷 추출기가 돌려주는 값 하나 (파이썬 olefile 이 주던 값의 모양)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
enum PVal {
    Int(i64),
    Bool(bool),
    Bytes(Vec<u8>),
    Str(String),
    // datetime — isoformat 글자로 들고 다닌다
    Date(String),
}

//------------------------------------------------------------------
// 값 → JSON (파이썬 _val)
//=> 바이트는 UTF-8 → CP949 순으로 풀고(구형 Office 는 시스템 코드페이지 = CP949),
//   글자는 첫 NUL 에서 자르고 앞뒤 공백을 걷는다. 날짜는 isoformat 글자 그대로.
//
// -in: v = 값
//
// -out: JSON 값
// -out: error = 없음 (못 푸는 바이트는 replace)
//------------------------------------------------------------------
fn val(v: &PVal) -> Value {
    match v {
        PVal::Int(i) => json!(i),
        PVal::Bool(b) => json!(b),
        PVal::Date(s) => json!(s),
        PVal::Str(s) => json!(cut(s)),
        PVal::Bytes(b) => {
            let s = pyfmt::decode_utf8(b).or_else(|| pyfmt::decode_cp949(b))
                .unwrap_or_else(|| String::from_utf8_lossy(b).into_owned());
            json!(cut(&s))
        }
    }
}

// 첫 NUL 앞 · 앞뒤 공백 걷기 — v.split("\x00", 1)[0].strip()
fn cut(s: &str) -> String {
    pyutil::strip(s.split('\0').next().unwrap_or("")).to_string()
}

// JSON 값에 _val 을 다시 한 번(extract_raw 의 마지막 정리) — 글자만 바뀐다
fn val_json(v: &Value) -> Value {
    match v {
        Value::String(s) => json!(cut(s)),
        Value::Array(a) => Value::Array(a.iter().map(|x| match x {
            Value::String(s) => json!(cut(s)),
            o => o.clone(),
        }).collect()),
        o => o.clone(),
    }
}

// 비었는가 — 파이썬 `v not in (None, "")` 의 반대
fn is_blank(v: &Value) -> bool {
    matches!(v, Value::Null) || matches!(v, Value::String(s) if s.is_empty())
}

//------------------------------------------------------------------
// 매직 바이트로 형식 판정 (§10-1)
//=> 확장자는 거짓말을 한다(.doc 인데 ZIP, 0바이트 등). 앞 바이트와 안쪽 구조로 정한다.
//    1) PK → [Content_Types].xml 로 docx/xlsx/pptx, mimetype 이 hwp+zip 이면 HWPX
//    2) OLE → 스트림 이름으로 doc/xls/ppt/hwp
//    3) %PDF → pdf, 0바이트 → empty
//    4) 그 밖은 확장자
//
// -in: path = 파일 경로 / ext = 소문자 확장자
//
// -out: (kind, format, status) — kind = ooxml|ole|hwp|hwpx|pdf|none(속성 없음)
// -out: error = 없음 (못 읽으면 확장자 판정)
//------------------------------------------------------------------
pub fn detect(path: &str, ext: &str) -> (&'static str, &'static str, &'static str) {
    let fe = format_of_ext(ext);
    let mut head = vec![0u8; 8];
    let n = match std::fs::File::open(path).and_then(|mut f| {
        let mut got = 0;
        while got < 8 {
            let k = f.read(&mut head[got..])?;
            if k == 0 {
                break;
            }
            got += k;
        }
        Ok(got)
    }) {
        Ok(n) => n,
        Err(_) => return ("none", fe, "failed"),
    };
    let head = &head[..n];
    if head.is_empty() {
        return ("none", fe, "empty");
    }
    if head.starts_with(b"PK\x03\x04") {
        let mut z = match std::fs::File::open(path).ok().and_then(|f| zip::ZipArchive::new(f).ok()) {
            Some(z) => z,
            None => return ("none", fe, "failed"),
        };
        let names: Vec<String> = z.file_names().map(|s| s.to_string()).collect();
        if names.iter().any(|n| n == "mimetype") {
            match zip_read(&mut z, "mimetype") {
                Some(b) => {
                    if b.to_ascii_lowercase().windows(3).any(|w| w == b"hwp") {
                        return ("hwpx", "hwp", "ok");
                    }
                }
                None => return ("none", fe, "failed"),
            }
        }
        if names.iter().any(|n| n == "[Content_Types].xml") {
            let ct = match zip_read(&mut z, "[Content_Types].xml") {
                Some(b) => String::from_utf8_lossy(&b).into_owned(),
                None => return ("none", fe, "failed"),
            };
            for (key, f) in [("wordprocessingml", "word"), ("spreadsheetml", "excel"), ("presentationml", "ppt")] {
                if ct.contains(key) {
                    return ("ooxml", f, "ok");
                }
            }
        }
        return ("none", fe, "ok");
    }
    if head.starts_with(b"\xd0\xcf\x11\xe0") {
        let ole = match cfb::open(path) {
            Ok(o) => o,
            Err(_) => return ("none", fe, "failed"),
        };
        if ole.exists("/FileHeader") {
            return ("hwp", "hwp", "ok");
        }
        if ole.exists("/WordDocument") {
            return ("ole", "word", "ok");
        }
        if ole.exists("/Workbook") || ole.exists("/Book") {
            return ("ole", "excel", "ok");
        }
        if ole.exists("/PowerPoint Document") {
            return ("ole", "ppt", "ok");
        }
        // 암호화된 OOXML 은 OLE 껍데기에 EncryptionInfo 만 있다
        if ole.exists("/EncryptionInfo") {
            return ("none", fe, "encrypted");
        }
        return ("ole", fe, "ok");
    }
    if head.starts_with(b"%PDF") {
        return ("pdf", "pdf", "ok");
    }
    ("none", fe, "ok")
}

// ZIP 안 파일 하나 통째로
fn zip_read(z: &mut zip::ZipArchive<std::fs::File>, name: &str) -> Option<Vec<u8>> {
    let mut f = z.by_name(name).ok()?;
    let mut b = vec![];
    f.read_to_end(&mut b).ok()?;
    Some(b)
}

//------------------------------------------------------------------
// URL 의 호스트 — 파이썬 urllib.parse.urlparse(v).hostname (소문자)
//
// -in: url = 글자
//
// -out: Some(호스트) / 없으면 None
// -out: error = 없음
//------------------------------------------------------------------
fn url_hostname(url: &str) -> Option<String> {
    // 앞쪽 제어 문자 · 공백, 안쪽 탭 · 줄바꿈은 파이썬처럼 걷는다
    let url: String = url.trim_start_matches(|c: char| (c as u32) <= 0x20).chars().filter(|c| !matches!(c, '\t' | '\r' | '\n')).collect();
    let mut rest = url.as_str();
    if let Some(i) = rest.find(':') {
        let sch = &rest[..i];
        let first = sch.chars().next();
        if i > 0 && first.map(|c| c.is_ascii_alphabetic()).unwrap_or(false)
            && sch.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)) {
            rest = &rest[i + 1..];
        }
    }
    if !rest.starts_with("//") {
        return None;
    }
    let body = &rest[2..];
    let end = body.find(['/', '?', '#']).unwrap_or(body.len());
    let netloc = &body[..end];
    let hostinfo = pyutil::rpartition(netloc, "@").2;
    let (_, br, bracketed) = pyutil::partition(hostinfo, "[");
    let host = if br { pyutil::partition(bracketed, "]").0 } else { pyutil::partition(hostinfo, ":").0 };
    if host.is_empty() {
        return None;
    }
    let (h, pct, zone) = pyutil::partition(host, "%");
    let out = format!("{}{}{}", pyfmt::lower(h), if pct { "%" } else { "" }, zone);
    // (urlparse(v).hostname or "").lower() or None
    let out = pyfmt::lower(&out);
    if out.is_empty() { None } else { Some(out) }
}

//------------------------------------------------------------------
// 파일 시스템 정보
//=> 모든 파일에 공통 — 크기 · 생성일(PC 에 들어온 날) · 수정일 · 내용 해시 · 받은 곳.
//   날짜는 파이썬 datetime.fromtimestamp(st, timezone.utc).isoformat() 과 같은 글자.
//   Zone.Identifier 는 도메인만 남긴다(전체 URL 의 쿼리에 토큰이 있을 수 있다, §15).
//
// -in: path = 파일 경로 / want_hash = 내용 해시를 셀지(색인이 이미 셌으면 false)
//
// -out: {size, created, modified, sha1, host, referrer_host}
// -out: error = stat 실패는 Err (해시 · Zone 실패는 null)
//------------------------------------------------------------------
pub fn fs_info(path: &str, want_hash: bool) -> std::io::Result<Map<String, Value>> {
    use std::os::windows::fs::MetadataExt;
    let st = std::fs::metadata(path)?;
    let mut info = Map::new();
    info.insert("size".into(), json!(st.file_size()));
    info.insert("created".into(), json!(pyfmt::filetime_iso_utc(st.creation_time())));
    info.insert("modified".into(), json!(pyfmt::filetime_iso_utc(st.last_write_time())));
    info.insert("sha1".into(), Value::Null);
    info.insert("host".into(), Value::Null);
    info.insert("referrer_host".into(), Value::Null);
    if want_hash {
        if let Ok(h) = crate::indexer::content_hash(path) {
            info.insert("sha1".into(), json!(h));
        }
    }
    // 인터넷 · 메일에서 받은 파일에만 Windows 가 붙이는 숨은 스트림(NTFS)
    if let Ok(b) = std::fs::read(format!("{}:Zone.Identifier", path)) {
        let text = String::from_utf8_lossy(&b).replace("\r\n", "\n").replace('\r', "\n");
        for line in text.split('\n') {
            let (k, _, v) = pyutil::partition(pyutil::strip(line), "=");
            if k == "HostUrl" && !v.is_empty() {
                info.insert("host".into(), url_hostname(v).map(Value::from).unwrap_or(Value::Null));
            } else if k == "ReferrerUrl" && !v.is_empty() {
                info.insert("referrer_host".into(), url_hostname(v).map(Value::from).unwrap_or(Value::Null));
            }
        }
    }
    Ok(info)
}

//------------------------------------------------------------------
// XML 한 단계 아래 자식을 {태그: 글} 로 펴기 (core.xml · app.xml)
//
// -in: data = XML 바이트
//
// -out: [(태그이름, 글)] — 같은 태그는 뒤 값이 이긴다
// -out: error = XML 이 깨졌으면 Err("ParseError: …")
//------------------------------------------------------------------
fn flat_xml(data: &[u8]) -> Result<Vec<(String, String)>, String> {
    let root = xmldom::parse(data).map_err(|e| format!("ParseError: {}", e))?;
    let mut out: Vec<(String, String)> = vec![];
    for c in &root.children {
        let t = pyutil::strip(&c.text).to_string();
        if !t.is_empty() {
            match out.iter_mut().find(|(k, _)| *k == c.tag) {
                Some(slot) => slot.1 = t,
                None => out.push((c.tag.clone(), t)),
            }
        }
    }
    Ok(out)
}

fn get<'a>(kv: &'a [(String, String)], k: &str) -> Option<&'a str> {
    kv.iter().find(|(a, _)| a == k).map(|(_, v)| v.as_str())
}

static RE_STYLE: Lazy<Regex> = Lazy::new(|| Regex::new(r#"(?s)<w:style\b[^>]*w:styleId="([^"]+)"[^>]*>.*?<w:name w:val="([^"]+)""#).unwrap());
static RE_PSTYLE: Lazy<Regex> = Lazy::new(|| Regex::new(r#"<w:pStyle w:val="([^"]+)""#).unwrap());
static RE_SHEET: Lazy<Regex> = Lazy::new(|| Regex::new(r#"<sheet\b[^>]*\bname="([^"]*)""#).unwrap());
static RE_SLIDE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^ppt/slides/slide(\d+)\.xml$").unwrap());
static RE_SP: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)<p:sp>.*?</p:sp>").unwrap());
static RE_PH_TITLE: Lazy<Regex> = Lazy::new(|| Regex::new(r#"<p:ph[^>]*type="(?:title|ctrTitle)""#).unwrap());
static RE_AT: Lazy<Regex> = Lazy::new(|| Regex::new(r"<a:t>([^<]*)</a:t>").unwrap());

// 순서 있는 속성 묶음(파이썬 dict 처럼 넣은 순서 유지)
type Doc = Vec<(String, Value)>;

fn put(m: &mut Doc, k: &str, v: Value) {
    match m.iter_mut().find(|(a, _)| a == k) {
        Some(slot) => slot.1 = v,
        None => m.push((k.to_string(), v)),
    }
}

//------------------------------------------------------------------
// OOXML(docx · xlsx · pptx) 속성
//=> core.xml(제목 · 만든 이 · 저장자 · 날짜) · app.xml(템플릿 · 쪽 · 슬라이드 · 프로그램) · 구조
//   (Word 제목 스타일 수, Excel 시트명, PPT 슬라이드 제목). 키워드 · 주제 · 설명 · 회사 ·
//   사용자 정의 속성 · 메모 · 변경 추적 작성자는 읽지 않는다(§5).
//   app.xml 의 Pages 는 글자 그대로("3") 둔다(파이썬과 같다).
//
// -in: path = 파일 경로
//
// -out: Ok([(키, 값)])
// -out: error = ZIP · XML 이 깨졌으면 Err
//------------------------------------------------------------------
fn ooxml_meta(path: &str) -> Result<Doc, String> {
    let f = std::fs::File::open(path).map_err(|e| format!("OSError: {}", e))?;
    let mut z = zip::ZipArchive::new(f).map_err(|e| format!("BadZipFile: {}", e))?;
    let names: std::collections::BTreeSet<String> = z.file_names().map(|s| s.to_string()).collect();
    let rd = |z: &mut zip::ZipArchive<std::fs::File>, n: &str| -> Result<Vec<u8>, String> {
        zip_read(z, n).ok_or_else(|| format!("BadZipFile: {} 를 읽지 못함", n))
    };
    let mut m: Doc = vec![];
    if names.contains("docProps/core.xml") {
        let c = flat_xml(&rd(&mut z, "docProps/core.xml")?)?;
        for (src, dst) in [("title", "title"), ("creator", "author"), ("lastModifiedBy", "last_saved_by"),
                           ("created", "created"), ("modified", "modified")] {
            if let Some(v) = get(&c, src) {
                put(&mut m, dst, json!(v));
            }
        }
    }
    if names.contains("docProps/app.xml") {
        let a = flat_xml(&rd(&mut z, "docProps/app.xml")?)?;
        for (src, dst) in [("Template", "template"), ("Pages", "pages"), ("Slides", "slides"), ("Application", "app")] {
            if let Some(v) = get(&a, src) {
                put(&mut m, dst, json!(v));
            }
        }
    }
    if names.contains("word/document.xml") && names.contains("word/styles.xml") {
        // 한글 Word 의 제목 스타일 ID 는 "1" 처럼 숫자라 styles.xml 의 이름으로 판별
        let sty = String::from_utf8_lossy(&rd(&mut z, "word/styles.xml")?).into_owned();
        let ids: std::collections::HashSet<String> = RE_STYLE.captures_iter(&sty).filter_map(|c| {
            let nm = pyfmt::lower(&c[2]);
            if nm.starts_with("heading") || nm == "title" { Some(c[1].to_string()) } else { None }
        }).collect();
        let doc = String::from_utf8_lossy(&rd(&mut z, "word/document.xml")?).into_owned();
        let n = RE_PSTYLE.captures_iter(&doc).filter(|c| ids.contains(&c[1])).count();
        put(&mut m, "headings", json!(n));
    }
    if names.contains("xl/workbook.xml") {
        let wb = String::from_utf8_lossy(&rd(&mut z, "xl/workbook.xml")?).into_owned();
        let sheets: Vec<Value> = RE_SHEET.captures_iter(&wb).map(|c| json!(&c[1])).collect();
        put(&mut m, "sheets", Value::Array(sheets));
    }
    let mut slides: Vec<(u64, String)> = names.iter().filter_map(|n| {
        RE_SLIDE.captures(n).and_then(|c| c[1].parse::<u64>().ok()).map(|k| (k, n.clone()))
    }).collect();
    slides.sort_by(|a, b| a.0.cmp(&b.0));
    if !slides.is_empty() {
        let mut titles = vec![];
        for (_, n) in &slides {
            let x = String::from_utf8_lossy(&rd(&mut z, n)?).into_owned();
            let mut t = String::new();
            // 제목 자리표시자(title · ctrTitle) 도형의 글만 슬라이드 제목으로 본다
            for sp in RE_SP.find_iter(&x) {
                let sp = sp.as_str();
                if RE_PH_TITLE.is_match(sp) {
                    let joined: String = RE_AT.captures_iter(sp).map(|c| c[1].to_string()).collect();
                    t = pyutil::strip(&joined).to_string();
                    break;
                }
            }
            titles.push(json!(t));
        }
        put(&mut m, "slides", json!(slides.len()));
        put(&mut m, "slide_titles", Value::Array(titles));
    }
    Ok(m)
}

// ── OLE 속성 집합(MS-OLEPS) — 파이썬 olefile.getproperties 를 그대로 ──

// 바이트 조각 → u16/u32 (모자라면 None — 파이썬 struct.error)
fn u16_at(s: &[u8], o: usize) -> Option<u32> {
    s.get(o..o + 2).map(|b| u16::from_le_bytes([b[0], b[1]]) as u32)
}
fn u32_at(s: &[u8], o: usize) -> Option<u32> {
    s.get(o..o + 4).map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
}

// 파이썬 s[a:b] (범위 밖은 잘라 맞춤)
fn sl(s: &[u8], a: usize, b: usize) -> &[u8] {
    let a = a.min(s.len());
    let b = b.min(s.len());
    if a >= b { &[] } else { &s[a..b] }
}

//------------------------------------------------------------------
// 속성 하나 풀기 — olefile _parse_property_basic
//=> 파이썬 판과 같은 값이 나오도록 olefile 의 동작을 그대로 따른다(버그까지).
//    · VT_I4 도 부호 없이 읽는다(i32 가 "<I")
//    · VT_LPWSTR 은 길이를 4바이트 뒤에서 읽어 구역 끝까지 읽힌다 → 뒤에서 첫 NUL 로 자른다
//    · VT_LPSTR 은 바이트 그대로(NUL 은 모두 지움) — 글자 풀기는 _val(UTF-8 → CP949)
//    · VT_FILETIME 은 1601-01-01 + 마이크로초 → isoformat (9999년을 넘으면 그 속성은 버림)
//
// -in: s = 구역 바이트("****" + 나머지) / off = 값 시작(형식 4바이트 뒤) / ptype = 형식 / convert = 날짜로 바꿀지
//
// -out: Some(Some(값)) / 모르는 형식은 Some(None) / 읽다가 모자라면 None(파이썬 예외 — 그 속성 버림)
// -out: error = 없음
//------------------------------------------------------------------
fn parse_prop(s: &[u8], off: usize, ptype: u32, convert: bool) -> Option<Option<PVal>> {
    const VT_I2: u32 = 2;
    const VT_I4: u32 = 3;
    const VT_BSTR: u32 = 8;
    const VT_ERROR: u32 = 10;
    const VT_BOOL: u32 = 11;
    const VT_UI1: u32 = 17;
    const VT_UI2: u32 = 18;
    const VT_UI4: u32 = 19;
    const VT_INT: u32 = 22;
    const VT_UINT: u32 = 23;
    const VT_LPSTR: u32 = 30;
    const VT_LPWSTR: u32 = 31;
    const VT_FILETIME: u32 = 64;
    const VT_BLOB: u32 = 65;
    const VT_CF: u32 = 71;
    const VT_CLSID: u32 = 72;
    if !(ptype <= VT_BLOB || ptype == VT_CLSID || ptype == VT_CF) {
        // 벡터 등 — 쓰는 속성에는 나오지 않는다
        return Some(None);
    }
    Some(Some(match ptype {
        VT_I2 => {
            let v = u16_at(s, off)? as i64;
            PVal::Int(if v >= 32768 { v - 65536 } else { v })
        }
        VT_UI2 => PVal::Int(u16_at(s, off)? as i64),
        VT_I4 | VT_INT | VT_ERROR | VT_UI4 | VT_UINT => PVal::Int(u32_at(s, off)? as i64),
        VT_BSTR | VT_LPSTR => {
            let count = u32_at(s, off)? as usize;
            let a = off + 4;
            let b = (a + count).saturating_sub(1).max(a);
            let v: Vec<u8> = sl(s, a, b).iter().copied().filter(|c| *c != 0).collect();
            PVal::Bytes(v)
        }
        VT_BLOB | VT_CF => {
            let count = u32_at(s, off)? as usize;
            PVal::Bytes(sl(s, off + 4, off + 4 + count).to_vec())
        }
        VT_LPWSTR => {
            let count = u32_at(s, off + 4)? as usize;
            let b = sl(s, off + 4, (off + 4).saturating_add(count.saturating_mul(2)));
            let units: Vec<u16> = b.chunks(2).filter(|c| c.len() == 2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
            let mut t: String = char::decode_utf16(units.iter().copied()).map(|r| r.unwrap_or('\u{FFFD}')).collect();
            if b.len() % 2 == 1 {
                t.push('\u{FFFD}');
            }
            PVal::Str(t)
        }
        VT_FILETIME => {
            let lo = u32_at(s, off)? as u64;
            let hi = u32_at(s, off + 4)? as u64;
            let ft = lo + (hi << 32);
            if convert {
                // datetime(1601,1,1) + timedelta(microseconds=ft//10)
                let us_1601 = (ft / 10) as i64;
                let epoch_1601_us = pyfmt::days_from_civil(1601, 1, 1) * 86_400_000_000;
                let us = epoch_1601_us + us_1601;
                // 9999-12-31 을 넘으면 OverflowError — 그 속성은 버린다
                if us >= (pyfmt::days_from_civil(10000, 1, 1) * 86_400_000_000) {
                    return None;
                }
                PVal::Date(pyfmt::isoformat_us(us, ""))
            } else {
                PVal::Int((ft / 10_000_000) as i64)
            }
        }
        VT_UI1 => PVal::Int(*s.get(off)? as i64),
        VT_BOOL => PVal::Bool(u16_at(s, off)? != 0),
        _ => return Some(None),
    }))
}

//------------------------------------------------------------------
// 속성 집합 스트림 읽기 — olefile getproperties(첫 구역만)
//=> 머리를 못 읽으면 빈 목록, 속성 하나가 깨지면 그것만 건너뛴다(파이썬과 같다).
//
// -in: data    = 스트림 바이트
// -in: want    = 이 PID 들만 푼다(나머지는 읽지 않는다 — HWP PID 6 설명란 등)
// -in: convert = FILETIME 을 날짜로 바꿀지
//
// -out: [(PID, 값)] — 스트림 순서, 같은 PID 는 뒤 값이 이긴다
// -out: error = 없음
//------------------------------------------------------------------
fn ole_props(data: &[u8], want: &[u32], convert: bool) -> Vec<(u32, Option<PVal>)> {
    let mut out: Vec<(u32, Option<PVal>)> = vec![];
    let head = (|| -> Option<Vec<u8>> {
        let s2 = data.get(28..48)?;
        let off = u32_at(s2, 16)? as usize;
        let size = u32_at(data, off)? as i64;
        let start = off + 4;
        // fp.read(n-4) — 음수면 끝까지
        let body = if size - 4 < 0 { sl(data, start, data.len()) } else { sl(data, start, start + (size - 4) as usize) };
        let mut s = b"****".to_vec();
        s.extend_from_slice(body);
        Some(s)
    })();
    let s = match head {
        Some(s) => s,
        None => return out,
    };
    let num = match u32_at(&s, 4) {
        Some(n) => (n as usize).min(s.len() / 8),
        None => return out,
    };
    for i in 0..num {
        let (pid, off) = match (u32_at(&s, 8 + i * 8), u32_at(&s, 12 + i * 8)) {
            (Some(p), Some(o)) => (p, o as usize),
            _ => continue,
        };
        if !want.contains(&pid) {
            continue;
        }
        let ptype = match u32_at(&s, off) {
            Some(t) => t,
            None => continue,
        };
        if let Some(v) = parse_prop(&s, off + 4, ptype, convert) {
            match out.iter_mut().find(|(p, _)| *p == pid) {
                Some(slot) => slot.1 = v,
                None => out.push((pid, v)),
            }
        }
    }
    out
}

// OLE 스트림 하나 통째로(없으면 None)
fn ole_stream(f: &mut cfb::CompoundFile<std::fs::File>, name: &str) -> Result<Option<Vec<u8>>, String> {
    if !f.is_stream(name) {
        return Ok(None);
    }
    let mut s = f.open_stream(name).map_err(|e| format!("OSError: {}", e))?;
    let mut b = vec![];
    s.read_to_end(&mut b).map_err(|e| format!("OSError: {}", e))?;
    Ok(Some(b))
}

//------------------------------------------------------------------
// 구형 Office(doc · xls · ppt) 속성 — OLE SummaryInformation (olefile get_metadata)
//=> 제목 · 작성자 · 템플릿 · 저장자 · 생성/저장일 · 쪽 · 프로그램 · 슬라이드 수 · 코드페이지.
//   메모(comments) · 키워드 · 주제 · 회사 · 관리자 · 범주는 읽지 않는다(§5).
//
// -in: path = 파일 경로
//
// -out: Ok([(키, 값)])
// -out: error = OLE 가 깨졌으면 Err
//------------------------------------------------------------------
fn ole_meta(path: &str) -> Result<Doc, String> {
    let mut f = cfb::open(path).map_err(|e| format!("OSError: {}", e))?;
    let summ = match ole_stream(&mut f, "/\u{5}SummaryInformation")? {
        Some(b) => ole_props(&b, &[1, 2, 4, 7, 8, 12, 13, 14, 18], true),
        None => vec![],
    };
    let docsum = match ole_stream(&mut f, "/\u{5}DocumentSummaryInformation")? {
        Some(b) => ole_props(&b, &[7], true),
        None => vec![],
    };
    let pick = |props: &[(u32, Option<PVal>)], pid: u32| props.iter().find(|(p, _)| *p == pid).and_then(|(_, v)| v.clone());
    let mut m: Doc = vec![];
    let fields: [(&str, Option<PVal>); 10] = [
        ("title", pick(&summ, 2)), ("author", pick(&summ, 4)), ("template", pick(&summ, 7)),
        ("last_saved_by", pick(&summ, 8)), ("created", pick(&summ, 12)), ("modified", pick(&summ, 13)),
        ("pages", pick(&summ, 14)), ("app", pick(&summ, 18)), ("slides", pick(&docsum, 7)), ("codepage", pick(&summ, 1)),
    ];
    for (dst, v) in fields {
        if let Some(v) = v {
            let j = val(&v);
            if !is_blank(&j) {
                put(&mut m, dst, j);
            }
        }
    }
    Ok(m)
}

// HWP 요약정보 속성 ID — 쓰는 것만(6 설명란은 DRM 추적 정보가 들어 있어 읽지 않는다, §15)
const HWP_PIDS: &[(u32, &str)] = &[(2, "title"), (4, "author"), (7, "template"), (8, "last_saved_by"), (12, "created"),
                                   (13, "modified"), (14, "pages"), (20, "date_str")];

//------------------------------------------------------------------
// HWP 5.0 속성 — \005HwpSummaryInformation + FileHeader 속성 비트
//=> olefile 이 HWP 문자열 길이를 잘못 읽어 뒤 속성까지 붙여 주므로 첫 NUL 에서 자른다(실측).
//   Rust 판도 같은 방식으로 읽어 같은 글자를 얻는다.
//
// -in: path = 파일 경로
//
// -out: Ok([(키, 값)]) — encrypted · distribution 이 먼저
// -out: error = OLE 가 깨졌으면 Err
//------------------------------------------------------------------
fn hwp_meta(path: &str) -> Result<Doc, String> {
    let mut f = cfb::open(path).map_err(|e| format!("OSError: {}", e))?;
    let mut m: Doc = vec![];
    if let Some(hdr) = ole_stream(&mut f, "/FileHeader")? {
        if hdr.len() >= 40 {
            let flags = u32::from_le_bytes([hdr[36], hdr[37], hdr[38], hdr[39]]);
            put(&mut m, "encrypted", json!(flags & 2 != 0));
            put(&mut m, "distribution", json!(flags & 4 != 0));
        }
    }
    if let Some(b) = ole_stream(&mut f, "/\u{5}HwpSummaryInformation")? {
        let want: Vec<u32> = HWP_PIDS.iter().map(|(p, _)| *p).collect();
        for (pid, v) in ole_props(&b, &want, true) {
            let key = match HWP_PIDS.iter().find(|(p, _)| *p == pid) {
                Some((_, k)) => *k,
                None => continue,
            };
            if let Some(v) = v {
                let j = val(&v);
                if !is_blank(&j) {
                    put(&mut m, key, j);
                }
            }
        }
    }
    Ok(m)
}

//------------------------------------------------------------------
// HWPX 속성 — Contents/content.hpf 의 <opf:meta> · <title>
//
// -in: path = 파일 경로
//
// -out: Ok([(키, 값)])
// -out: error = ZIP · XML 이 깨졌으면 Err
//------------------------------------------------------------------
fn hwpx_meta(path: &str) -> Result<Doc, String> {
    let f = std::fs::File::open(path).map_err(|e| format!("OSError: {}", e))?;
    let mut z = zip::ZipArchive::new(f).map_err(|e| format!("BadZipFile: {}", e))?;
    let mut m: Doc = vec![];
    if !z.file_names().any(|n| n == "Contents/content.hpf") {
        return Ok(m);
    }
    let data = zip_read(&mut z, "Contents/content.hpf").ok_or("BadZipFile: content.hpf 를 읽지 못함")?;
    let root = xmldom::parse(&data).map_err(|e| format!("ParseError: {}", e))?;
    let pick = [("creator", "author"), ("lastsaveby", "last_saved_by"), ("CreatedDate", "created"), ("ModifiedDate", "modified")];
    for el in root.iter() {
        let t = pyutil::strip(&el.text).to_string();
        if t.is_empty() {
            continue;
        }
        if el.tag == "meta" {
            // el.get("name") — 접두사 없는 name 속성만
            let nm = el.attrs.iter().find(|(k, _)| k == "name").map(|(_, v)| v.as_str());
            if let Some(dst) = nm.and_then(|n| pick.iter().find(|(s, _)| *s == n)).map(|(_, d)| *d) {
                put(&mut m, dst, json!(t));
            }
        } else if el.tag == "title" {
            put(&mut m, "title", json!(t));
        }
    }
    Ok(m)
}

//------------------------------------------------------------------
// PDF 속성 — Info 사전 · 쪽 수 · 앞 3쪽 글자 수(스캔 판정) · 책갈피 수
//
// -in: path = 파일 경로
//
// -out: Ok([(키, 값)])
// -out: error = 암호 · 손상 PDF 면 Err("PdfiumError: …")
//------------------------------------------------------------------
fn pdf_meta(path: &str) -> Result<Doc, String> {
    let p = crate::pdfium::meta_info(path)?;
    let mut m: Doc = vec![];
    for (src, dst) in [("Title", "title"), ("Author", "author"), ("Creator", "creator"), ("Producer", "producer"),
                       ("CreationDate", "created"), ("ModDate", "modified")] {
        if let Some((_, v)) = p.info.iter().find(|(k, _)| k == src) {
            put(&mut m, dst, json!(cut(v)));
        }
    }
    put(&mut m, "pages", json!(p.pages));
    put(&mut m, "toc", json!(p.toc));
    put(&mut m, "text_chars", json!(p.text_chars));
    Ok(m)
}

//------------------------------------------------------------------
// 파일 하나의 원본 메타데이터 (RawMeta)
//=> 형식 판정 → 파일 시스템 정보 → 포맷별 추출. 추출 실패는 status 로 남긴다.
//   이미지 · 텍스트 · 기타는 파일 시스템 정보만(결정 D6).
//
// -in: path = 파일 경로 / sha1 = 색인이 이미 센 내용 해시(있으면 다시 세지 않음)
//
// -out: raw = {name, ext, kind, format, ext_mismatch, status, error, fs, doc}
// -out: error = 파일이 없으면 Err(io) (속성 추출 실패는 raw["status"] = "failed")
//------------------------------------------------------------------
pub fn extract_raw(path: &str, sha1: Option<&str>) -> std::io::Result<Value> {
    let name = pyutil::basename(path).to_string();
    let ext = pyutil::lstrip_chars(&pyutil::splitext(&name).1.to_lowercase(), ".").to_string();
    let (kind, fmt, status) = detect(path, &ext);
    let mut fs = fs_info(path, sha1.is_none())?;
    if let Some(h) = sha1.filter(|h| !h.is_empty()) {
        fs.insert("sha1".into(), json!(h));
    }
    let mut raw = Map::new();
    raw.insert("name".into(), json!(name));
    raw.insert("ext".into(), json!(ext));
    raw.insert("kind".into(), json!(kind));
    raw.insert("format".into(), json!(fmt));
    raw.insert("ext_mismatch".into(), json!(format_of_ext(&ext) != fmt && fmt != "other"));
    raw.insert("status".into(), json!(status));
    raw.insert("error".into(), Value::Null);
    raw.insert("fs".into(), Value::Object(fs));
    raw.insert("doc".into(), json!({}));
    let fun: Option<fn(&str) -> Result<Doc, String>> = match kind {
        "ooxml" => Some(ooxml_meta),
        "ole" => Some(ole_meta),
        "hwp" => Some(hwp_meta),
        "hwpx" => Some(hwpx_meta),
        "pdf" => Some(pdf_meta),
        _ => None,
    };
    if let (Some(fun), "ok") = (fun, status) {
        match fun(path) {
            Ok(m) => {
                let mut d = Map::new();
                for (k, v) in m {
                    d.insert(k, val_json(&v));
                }
                let enc = d.get("encrypted").map(|v| pyfmt::truthy(Some(v))).unwrap_or(false);
                raw.insert("doc".into(), Value::Object(d));
                if enc {
                    raw.insert("status".into(), json!("encrypted"));
                }
            }
            Err(e) => {
                // 속성을 못 읽어도 파일 시스템 정보로 쓴다 — "종류: 내용" 의 내용 120자
                let (k, msg) = match e.split_once(": ") {
                    Some((k, m)) if !k.contains(' ') => (k.to_string(), m.to_string()),
                    _ => ("Exception".to_string(), e.clone()),
                };
                let msg = pyutil::head(&msg, 120).to_string();
                let low = msg.to_lowercase();
                let st = if low.contains("password") || low.contains("encrypt") { "encrypted" } else { "failed" };
                raw.insert("status".into(), json!(st));
                raw.insert("error".into(), json!(format!("{}: {}", k, msg)));
            }
        }
    }
    Ok(Value::Object(raw))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_host_rules() {
        assert_eq!(url_hostname("https://Mail.Naver.com:443/a?b=1"), Some("mail.naver.com".into()));
        assert_eq!(url_hostname("about:internet"), None);
        assert_eq!(url_hostname("https://user@[::1]:80/x"), Some("::1".into()));
        assert_eq!(format_of_ext("hwpx"), "hwp");
    }

    #[test]
    fn ole_lpwstr_reads_to_section_end() {
        // 구역: 크기 · 속성 1개(PID 2 → 오프셋 16) · VT_LPWSTR "AB" 뒤에 다른 값
        let mut sec = vec![];
        sec.extend_from_slice(&40u32.to_le_bytes());   // 구역 크기
        sec.extend_from_slice(&1u32.to_le_bytes());    // 속성 수
        sec.extend_from_slice(&2u32.to_le_bytes());    // PID
        sec.extend_from_slice(&16u32.to_le_bytes());   // 오프셋
        sec.extend_from_slice(&31u32.to_le_bytes());   // VT_LPWSTR
        sec.extend_from_slice(&3u32.to_le_bytes());    // 글자 수(무시된다)
        for u in ['A' as u16, 'B' as u16, 0, 'Z' as u16] {
            sec.extend_from_slice(&u.to_le_bytes());
        }
        sec.resize(40, 0);
        let mut data = vec![0u8; 48];
        data[28 + 16..28 + 20].copy_from_slice(&48u32.to_le_bytes());
        data.extend_from_slice(&sec);
        let p = ole_props(&data, &[2], true);
        let v = val(p[0].1.as_ref().unwrap());
        assert_eq!(v, json!("AB"));
    }
}
