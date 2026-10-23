//------------------------------------------------------------------
// 기존 추출기 (csoclassify.extract 의 HybridExtractor 와 같은 동작)
//=> Python 판 SimpleRAG 는 형제 프로젝트(MpowerClassify)의 csoclassify.extract 를 그대로 쓴다.
//   blocks 엔진에서도 구조 파서가 없는 포맷(hwp·ppt·xls·txt·py)과 파서 실패 시 폴백에 쓰인다.
//    1) 파일 머리(매직 바이트)로 형식을 정한다(detect)
//    2) 그 형식 엔진 → 실패·빈 결과·너무 짧으면 사이냅(snf) 폴백 순으로 시도한다
//    3) 모두 짧기만 하면 그중 가장 긴 결과를 쓴다
//------------------------------------------------------------------

use std::io::Read;

use crate::pyutil;
use crate::xmldom;

// 추출 결과가 이보다 짧으면(공백 제외 글자) 다음 엔진으로 넘어간다(CSOCLASSIFY_MIN_TEXT_LEN)
fn min_text_len() -> usize {
    std::env::var("CSOCLASSIFY_MIN_TEXT_LEN").ok().and_then(|v| v.parse().ok()).unwrap_or(20)
}
// 파일 크기 상한(바이트) — 100MB
fn max_file_bytes() -> u64 {
    std::env::var("CSOCLASSIFY_MAX_FILE_BYTES").ok().and_then(|v| v.parse().ok()).unwrap_or(100 * 1024 * 1024)
}
fn max_file_bytes_text() -> u64 {
    std::env::var("CSOCLASSIFY_MAX_FILE_BYTES_TEXT").ok().and_then(|v| v.parse().ok()).unwrap_or(100 * 1024 * 1024)
}
fn parser_timeout() -> u64 {
    std::env::var("CSOCLASSIFY_PARSER_TIMEOUT").ok().and_then(|v| v.parse().ok()).unwrap_or(60)
}
fn max_pdf_pages() -> usize {
    std::env::var("CSOCLASSIFY_MAX_PDF_PAGES").ok().and_then(|v| v.parse().ok()).unwrap_or(3000)
}

const OLE_MAGIC: &[u8] = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1";
const ZIP_MAGIC: &[u8] = b"PK\x03\x04";

// ── 형식 판정 (detect.py) ─────────────────────────────────

//------------------------------------------------------------------
// OLE 복합 문서 안을 보고 형식 정하기
//=> HWP5(FileHeader 서명) / doc(WordDocument) / xls(Workbook·Book) / ppt
//------------------------------------------------------------------
fn detect_ole(path: &str) -> &'static str {
    let mut f = match cfb::open(path) {
        Ok(f) => f,
        Err(_) => return "unknown",
    };
    let top: Vec<String> = match f.read_root_storage() {
        it => it.map(|e| e.name().to_string()).collect(),
    };
    if top.iter().any(|n| n == "FileHeader") {
        if let Ok(mut s) = f.open_stream("/FileHeader") {
            let mut head = [0u8; 32];
            let n = s.read(&mut head).unwrap_or(0);
            if head[..n].starts_with(b"HWP Document File") {
                return "hwp";
            }
        }
    }
    if top.iter().any(|n| n == "WordDocument") {
        return "doc";
    }
    if top.iter().any(|n| n == "Workbook" || n == "Book") {
        return "xls";
    }
    if top.iter().any(|n| n == "PowerPoint Document") {
        return "ppt";
    }
    "ole"
}

// ZIP 안 이름으로 OOXML·HWPX 가리기
fn detect_zip(path: &str) -> &'static str {
    let f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return "unknown",
    };
    let mut z = match zip::ZipArchive::new(f) {
        Ok(z) => z,
        Err(_) => return "unknown",
    };
    let names: Vec<String> = z.file_names().map(|s| s.to_string()).collect();
    let has = |n: &str| names.iter().any(|x| x == n);
    if has("word/document.xml") {
        return "docx";
    }
    if has("xl/workbook.xml") {
        return "xlsx";
    }
    if has("ppt/presentation.xml") {
        return "pptx";
    }
    if names.iter().any(|n| n.starts_with("Contents/section")) {
        return "hwpx";
    }
    if has("mimetype") {
        if let Ok(mut e) = z.by_name("mimetype") {
            let mut b = vec![];
            if e.read_to_end(&mut b).is_ok() && b.windows(3).any(|w| w == b"hwp") {
                return "hwpx";
            }
        }
    }
    "zip"
}

//------------------------------------------------------------------
// 파일 형식 판정 — SimpleRAG extract.make_detector 와 같다
//=> OLE 면 안을 열어 정확히 보고, 아니면 머리 512바이트로 가른다.
//------------------------------------------------------------------
pub fn detect(path: &str) -> &'static str {
    let mut head = vec![0u8; 512];
    let n = match std::fs::File::open(path).and_then(|mut f| f.read(&mut head)) {
        Ok(n) => n,
        Err(_) => return "unknown",
    };
    head.truncate(n);
    if head.is_empty() {
        return "unknown";
    }
    if head.starts_with(b"%PDF-") {
        return "pdf";
    }
    if head.starts_with(b"\x89PNG\r\n\x1a\n") || head.starts_with(b"\xFF\xD8\xFF")
        || head.starts_with(b"GIF87a") || head.starts_with(b"GIF89a") || head.starts_with(b"BM") {
        return "image";
    }
    if head.starts_with(OLE_MAGIC) {
        return detect_ole(path);
    }
    if head.starts_with(ZIP_MAGIC) {
        return detect_zip(path);
    }
    if !head.contains(&0u8) {
        let low = head.to_ascii_lowercase();
        let has = |s: &[u8]| low.windows(s.len()).any(|w| w == s);
        if has(b"<!doctype html") || has(b"<html") || has(b"<head") || has(b"<body") {
            return "html";
        }
        return "text";
    }
    "binary"
}

// ── 엔진들 ─────────────────────────────────────────────────

//------------------------------------------------------------------
// 바이트 → 글자 (utf-8-sig → utf-8 → cp949 → latin-1 대체)
//------------------------------------------------------------------
pub fn decode_text(raw: &[u8]) -> String {
    let body = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw);
    if let Ok(s) = std::str::from_utf8(body) {
        return s.to_string();
    }
    // cp949 는 엄격하게 — 틀린 바이트가 있으면 다음 후보로
    let (s, _, bad) = encoding_rs::EUC_KR.decode(raw);
    if !bad {
        return s.into_owned();
    }
    raw.iter().map(|&b| b as char).collect()
}

// 일반 텍스트
fn plaintext(path: &str) -> Result<String, String> {
    let raw = std::fs::read(path).map_err(|e| format!("텍스트 읽기 실패: {}", io_kind(&e)))?;
    Ok(decode_text(&raw))
}

//------------------------------------------------------------------
// HTML 글자만 (기존 HtmlTextExtractor)
//=> script·style·head·noscript 안은 버리고, 블록 태그 앞뒤에 줄바꿈을 넣는다.
//------------------------------------------------------------------
fn html_text(path: &str) -> Result<String, String> {
    use crate::htmlparse::Ev;
    let raw = std::fs::read(path).map_err(|e| format!("HTML 읽기 실패: {}", io_kind(&e)))?;
    let text = decode_text(&raw);
    const DROP: &[&str] = &["script", "style", "head", "noscript"];
    const BLOCK: &[&str] = &["p", "div", "br", "tr", "td", "th", "li", "table", "h1", "h2", "h3", "h4",
        "h5", "h6", "ul", "ol", "section", "article", "header", "footer", "hr"];
    let mut parts = String::new();
    let mut skip = 0usize;
    for ev in crate::htmlparse::tokenize(&text) {
        match ev {
            // 스스로 닫는 태그도 여는 태그로만 처리(Python 판 handle_startendtag 재정의와 같다)
            Ev::Start(t, _, _) => {
                if DROP.contains(&t.as_str()) {
                    skip += 1;
                } else if BLOCK.contains(&t.as_str()) {
                    parts.push('\n');
                }
            }
            Ev::End(t) => {
                if DROP.contains(&t.as_str()) && skip > 0 {
                    skip -= 1;
                } else if BLOCK.contains(&t.as_str()) {
                    parts.push('\n');
                }
            }
            Ev::Data(d) => {
                if skip == 0 {
                    parts.push_str(&d);
                }
            }
        }
    }
    Ok(parts)
}

//------------------------------------------------------------------
// PDF 쪽 글자 이어 붙이기 (PdfiumExtractor)
//------------------------------------------------------------------
fn pdf_text(path: &str) -> Result<String, String> {
    let t0 = std::time::Instant::now();
    let max_pages = max_pdf_pages();
    let limit = parser_timeout();
    let (pages, _) = crate::pdfium::read_pages(path, false, |i, _| {
        !(max_pages > 0 && i >= max_pages) && !(limit > 0 && t0.elapsed().as_secs() >= limit)
    })
    .map_err(|e| if e.contains("적재") { e } else { format!("PDF 열기 실패: {}", e) })?;
    Ok(pages.iter().map(|p| p.text.as_str()).collect::<Vec<_>>().join("\n"))
}

// OLE 스트림 하나 통째로 읽기
fn ole_stream(f: &mut cfb::CompoundFile<std::fs::File>, name: &str) -> Option<Vec<u8>> {
    let mut s = f.open_stream(name).ok()?;
    let mut b = vec![];
    s.read_to_end(&mut b).ok()?;
    Some(b)
}

//------------------------------------------------------------------
// HWP5 본문 (Hwp5Extractor)
//=> BodyText/SectionN 스트림(대개 raw deflate 압축)의 PARA_TEXT(태그 67) 레코드를 모은다.
//------------------------------------------------------------------
fn hwp5_text(path: &str) -> Result<String, String> {
    let mut f = cfb::open(path).map_err(|_| "HWP5(OLE) 형식 아님".to_string())?;
    let header = ole_stream(&mut f, "/FileHeader").ok_or("HWP5 FileHeader 없음")?;
    let flags = if header.len() >= 40 { u32::from_le_bytes([header[36], header[37], header[38], header[39]]) } else { 0 };
    let compressed = flags & 1 != 0;
    if flags & 2 != 0 {
        return Err("HWP5 암호화 문서".into());
    }
    let mut secs: Vec<(i64, String)> = vec![];
    if let Ok(entries) = f.read_storage("/BodyText") {
        for e in entries {
            let name = e.name().to_string();
            if name.starts_with("Section") {
                let digits: String = name.chars().filter(|c| c.is_ascii_digit()).collect();
                secs.push((digits.parse().unwrap_or(0), name));
            }
        }
    }
    secs.sort();
    let mut parts = vec![];
    for (_, name) in secs {
        let mut data = match ole_stream(&mut f, &format!("/BodyText/{}", name)) {
            Some(d) => d,
            None => continue,
        };
        if compressed {
            let mut out = vec![];
            let mut dec = flate2::read::DeflateDecoder::new(&data[..]);
            if dec.read_to_end(&mut out).is_err() {
                continue;
            }
            data = out;
        }
        // 레코드 훑기: 머리 4바이트(태그 10비트, 크기 12비트, 0xFFF 면 다음 4바이트가 크기)
        let mut i = 0;
        while i + 4 <= data.len() {
            let h = u32::from_le_bytes([data[i], data[i + 1], data[i + 2], data[i + 3]]);
            i += 4;
            let tag = h & 0x3FF;
            let mut size = ((h >> 20) & 0xFFF) as usize;
            if size == 0xFFF {
                if i + 4 > data.len() {
                    break;
                }
                size = u32::from_le_bytes([data[i], data[i + 1], data[i + 2], data[i + 3]]) as usize;
                i += 4;
            }
            let end = (i + size).min(data.len());
            if tag == 67 {
                parts.push(decode_para_text(&data[i..end]));
            }
            i += size;
        }
    }
    Ok(parts.join("\n"))
}

// PARA_TEXT 의 UTF-16 글자 — 0·10·13 은 짧은 컨트롤, 그 밖의 32 미만은 16바이트 확장 컨트롤
fn decode_para_text(p: &[u8]) -> String {
    let mut units: Vec<u16> = vec![];
    let mut out = String::new();
    let mut i = 0;
    while i + 2 <= p.len() {
        let wc = u16::from_le_bytes([p[i], p[i + 1]]);
        if wc == 0 || wc == 10 || wc == 13 {
            if wc != 0 {
                out.push_str(&utf16_py(&units));
                units.clear();
                out.push('\n');
            }
            i += 2;
        } else if wc < 32 {
            i += 16;
        } else {
            units.push(wc);
            i += 2;
        }
    }
    out.push_str(&utf16_py(&units));
    out
}

// 파이썬 chr(wc) 를 하나씩 붙인 것과 같게 — Python 은 서로게이트 반쪽을 따로따로 str 에 넣고
// 뒤의 sanitize 가 그 반쪽들을 모두 지운다. 그래서 BMP 밖 글자(사설 영역 기호 등)는 통째로 사라진다
fn utf16_py(u: &[u16]) -> String {
    u.iter().filter(|&&x| !(0xD800..=0xDFFF).contains(&x)).filter_map(|&x| char::from_u32(x as u32)).collect()
}

//------------------------------------------------------------------
// DOC 본문 (DocExtractor) — piece table 로 글자 위치를 찾아 이어 붙인다
//------------------------------------------------------------------
fn doc_text(path: &str) -> Result<String, String> {
    let fail = |_| "doc 열기 실패: OSError".to_string();
    let mut f = cfb::open(path).map_err(fail)?;
    let wd = ole_stream(&mut f, "/WordDocument").ok_or("doc 열기 실패: OSError")?;
    let u16at = |b: &[u8], o: usize| -> Option<u16> { b.get(o..o + 2).map(|s| u16::from_le_bytes([s[0], s[1]])) };
    let u32at = |b: &[u8], o: usize| -> Option<u32> { b.get(o..o + 4).map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]])) };
    let flags = u16at(&wd, 0x0A).ok_or("doc 열기 실패: error")?;
    let mut tbl_name = if flags & 0x0200 != 0 { "1Table" } else { "0Table" };
    if !f.exists(format!("/{}", tbl_name)) {
        tbl_name = if f.exists("/1Table") { "1Table" } else { "0Table" };
    }
    let tbl = ole_stream(&mut f, &format!("/{}", tbl_name)).ok_or("doc 열기 실패: OSError")?;
    let fc_clx = u32at(&wd, 0x01A2).ok_or("doc 열기 실패: error")? as usize;
    let lcb_clx = u32at(&wd, 0x01A6).ok_or("doc 열기 실패: error")? as usize;
    let clx = tbl.get(fc_clx..(fc_clx + lcb_clx).min(tbl.len())).unwrap_or(&[]);
    let pcd = find_pcdt(clx);
    if pcd.is_empty() {
        return Err("doc piece table 없음".into());
    }
    let n_pieces = (pcd.len() as i64 - 4) / 12;
    if n_pieces <= 0 {
        return Err("doc piece 수 0".into());
    }
    let n = n_pieces as usize;
    let cps: Vec<u32> = (0..=n).map(|k| u32at(pcd, k * 4).unwrap_or(0)).collect();
    let base = (n + 1) * 4;
    let mut out = String::new();
    for k in 0..n {
        let fc = u32at(pcd, base + k * 8 + 2).unwrap_or(0);
        let n_ch = cps[k + 1].saturating_sub(cps[k]) as usize;
        if fc & 0x4000_0000 != 0 {
            let off = ((fc & 0x3FFF_FFFF) / 2) as usize;
            let seg = wd.get(off.min(wd.len())..(off + n_ch).min(wd.len())).unwrap_or(&[]);
            out.push_str(&encoding_rs::EUC_KR.decode_without_bom_handling(seg).0);
        } else {
            let off = fc as usize;
            let seg = wd.get(off.min(wd.len())..(off + n_ch * 2).min(wd.len())).unwrap_or(&[]);
            let u: Vec<u16> = seg.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
            out.push_str(&String::from_utf16_lossy(&u));
        }
    }
    for (a, b) in [("\x07", "\t"), ("\x0b", "\n"), ("\r", "\n"), ("\x08", ""), ("\x01", ""),
                   ("\x02", ""), ("\x13", ""), ("\x14", ""), ("\x15", "")] {
        out = out.replace(a, b);
    }
    Ok(out)
}

// CLX 에서 Pcdt(조각표) 부분만 꺼낸다
fn find_pcdt(clx: &[u8]) -> &[u8] {
    let mut i = 0;
    while i < clx.len() {
        match clx[i] {
            0x01 => {
                if i + 3 > clx.len() {
                    break;
                }
                let cb = u16::from_le_bytes([clx[i + 1], clx[i + 2]]) as usize;
                i += 3 + cb;
            }
            0x02 => {
                if i + 5 > clx.len() {
                    break;
                }
                let lcb = u32::from_le_bytes([clx[i + 1], clx[i + 2], clx[i + 3], clx[i + 4]]) as usize;
                return &clx[(i + 5).min(clx.len())..(i + 5 + lcb).min(clx.len())];
            }
            _ => break,
        }
    }
    &[]
}

//------------------------------------------------------------------
// PPT 본문 (PptExtractor) — 레코드 트리를 돌며 TextChars/TextBytes 를 모은다
//------------------------------------------------------------------
fn ppt_text(path: &str) -> Result<String, String> {
    let mut f = cfb::open(path).map_err(|_| "ppt 열기 실패: OSError".to_string())?;
    if !f.exists("/PowerPoint Document") {
        return Err("PowerPoint Document 스트림 없음".into());
    }
    let data = ole_stream(&mut f, "/PowerPoint Document").ok_or("ppt 열기 실패: OSError")?;
    let mut out = vec![];
    ppt_walk(&data, &mut out);
    let txt = out.into_iter().filter(|t| !pyutil::strip(t).is_empty()).collect::<Vec<_>>().join("\n");
    Ok(txt.replace('\x0b', "\n").replace('\r', "\n"))
}

fn ppt_walk(data: &[u8], out: &mut Vec<String>) {
    let mut i = 0;
    while i + 8 <= data.len() {
        let ver_inst = u16::from_le_bytes([data[i], data[i + 1]]);
        let rec_type = u16::from_le_bytes([data[i + 2], data[i + 3]]);
        let rec_len = u32::from_le_bytes([data[i + 4], data[i + 5], data[i + 6], data[i + 7]]) as usize;
        i += 8;
        let body = &data[i.min(data.len())..(i + rec_len).min(data.len())];
        if ver_inst & 0x0F == 0x0F {
            ppt_walk(body, out);
        } else if rec_type == 0x0FA0 {
            let u: Vec<u16> = body.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
            out.push(String::from_utf16_lossy(&u));
        } else if rec_type == 0x0FA8 {
            out.push(encoding_rs::EUC_KR.decode_without_bom_handling(body).0.into_owned());
        }
        i += rec_len;
    }
}

//------------------------------------------------------------------
// 숫자 셀 글자 — 정수면 정수로, 아니면 파이썬 repr(float)
//------------------------------------------------------------------
pub fn num_str(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e15 {
        format!("{}", f as i64)
    } else {
        pyutil::float_repr(f)
    }
}

//------------------------------------------------------------------
// XLS 셀 격자 (XlsExtractor — xlrd 와 같은 모양)
//=> 시트마다 0행 0열부터 마지막 행·열까지 모든 칸을 탭으로 잇는다(빈 칸도 자리 유지).
//------------------------------------------------------------------
fn xls_text(path: &str) -> Result<String, String> {
    use calamine::{open_workbook, Data, Reader, Xls};
    let mut wb: Xls<_> = open_workbook(path).map_err(|_| "xls 열기 실패: XLRDError".to_string())?;
    let mut out: Vec<String> = vec![];
    let names = wb.sheet_names().to_vec();
    for name in names {
        let range = match wb.worksheet_range(&name) {
            Ok(r) => r,
            Err(_) => continue,
        };
        let (sr, sc) = match range.start() {
            Some(s) => (s.0 as usize, s.1 as usize),
            None => continue,
        };
        let (er, ec) = range.end().map(|e| (e.0 as usize, e.1 as usize)).unwrap_or((sr, sc));
        let ncols = ec + 1;
        for r in 0..=er {
            let mut cells: Vec<String> = vec![String::new(); ncols];
            if r >= sr {
                for c in sc..=ec {
                    let v = range.get_value((r as u32, c as u32));
                    cells[c] = match v {
                        None | Some(Data::Empty) => String::new(),
                        Some(Data::String(s)) => s.clone(),
                        Some(Data::Float(f)) => num_str(*f),
                        Some(Data::Int(i)) => i.to_string(),
                        Some(Data::Bool(b)) => if *b { "1".into() } else { "0".into() },
                        Some(Data::DateTime(d)) => xls_date(d.as_f64()),
                        Some(Data::DateTimeIso(s)) | Some(Data::DurationIso(s)) => s.clone(),
                        Some(Data::Error(e)) => format!("{:?}", e),
                    };
                }
            }
            out.push(cells.join("\t"));
        }
    }
    Ok(out.join("\n"))
}

// xlrd 날짜 셀 글자 — 날짜만·시각만·둘 다
fn xls_date(v: f64) -> String {
    let days = v.floor();
    let secs = xlsdate_round(v - days);
    let has_time = secs != 0;
    if days == 0.0 {
        return crate::xlsdate::serial_f64(v, crate::xlsdate::KIND_TIME).unwrap_or_else(|| pyutil::float_repr(v));
    }
    let kind = if has_time { crate::xlsdate::KIND_DATETIME } else { crate::xlsdate::KIND_DATE };
    crate::xlsdate::serial_f64(v, kind).unwrap_or_else(|| pyutil::float_repr(v))
}
fn xlsdate_round(frac: f64) -> i64 {
    (frac * 86400.0).round() as i64 % 86400
}

// ── OOXML·HWPX (기존 경로 — 구조 파서 실패 시 폴백) ─────────

// ZIP 열기 + 항목 읽기 도우미
fn zip_open(path: &str, what: &str) -> Result<zip::ZipArchive<std::fs::File>, String> {
    let f = std::fs::File::open(path).map_err(|_| "입력 파일 없음".to_string())?;
    zip::ZipArchive::new(f).map_err(|_| format!("{} 열기 실패: BadZipFile", what))
}
pub fn zip_read(z: &mut zip::ZipArchive<std::fs::File>, name: &str) -> Option<Vec<u8>> {
    let mut e = z.by_name(name).ok()?;
    let mut b = vec![];
    e.read_to_end(&mut b).ok()?;
    Some(b)
}

// 문단 안 런 글자 모으기(_para_runs) — 중첩 문단은 건너뛴다
fn para_runs(el: &xmldom::Node, buf: &mut String, in_run: bool) {
    for ch in &el.children {
        match ch.tag.as_str() {
            "p" => continue,
            "r" => para_runs(ch, buf, true),
            "t" if in_run => buf.push_str(&ch.text),
            "tab" if in_run => buf.push('\t'),
            _ => para_runs(ch, buf, in_run),
        }
    }
}

// DOCX 본문 + 머리말·꼬리말
fn docx_text(path: &str) -> Result<String, String> {
    let mut z = zip_open(path, "docx")?;
    let data = zip_read(&mut z, "word/document.xml").ok_or("docx 본문(word/document.xml) 없음")?;
    let mut extras: Vec<String> = z.file_names()
        .filter(|n| n.ends_with(".xml") && (n.starts_with("word/header") || n.starts_with("word/footer")))
        .map(|s| s.to_string()).collect();
    extras.sort();
    let root = xmldom::parse(&data).map_err(|_| "docx XML 파싱 실패: ParseError".to_string())?;
    let mut lines = vec![];
    let collect = |root: &xmldom::Node, lines: &mut Vec<String>| {
        for p in root.iter() {
            if p.tag != "p" {
                continue;
            }
            let mut buf = String::new();
            para_runs(p, &mut buf, false);
            lines.push(buf);
        }
    };
    collect(&root, &mut lines);
    for n in extras {
        if let Some(b) = zip_read(&mut z, &n) {
            if let Ok(r) = xmldom::parse(&b) {
                collect(&r, &mut lines);
            }
        }
    }
    Ok(lines.join("\n"))
}

// 이름 끝 숫자(정렬용)
fn num_key(name: &str, re: &regex::Regex) -> i64 {
    re.captures(name).and_then(|c| c.get(1)).and_then(|m| m.as_str().parse().ok()).unwrap_or(0)
}

// XLSX 셀 값 + 메모
fn xlsx_text(path: &str) -> Result<String, String> {
    let re_sheet = regex::Regex::new(r"(?i)xl/worksheets/sheet(\d+)\.xml$").unwrap();
    let re_c = regex::Regex::new(r"(?i)xl/comments(\d*)\.xml$").unwrap();
    let re_tc = regex::Regex::new(r"(?i)xl/threadedComments/threadedComment(\d*)\.xml$").unwrap();
    let mut z = zip_open(path, "xlsx")?;
    let names: Vec<String> = z.file_names().map(|s| s.to_string()).collect();
    let shared = xlsx_shared(&mut z, &names);
    let styles = xlsx_date_styles(&mut z, &names);
    let mut sheets: Vec<String> = names.iter().filter(|n| re_sheet.is_match(n)).cloned().collect();
    sheets.sort_by_key(|n| num_key(n, &re_sheet));
    let t0 = std::time::Instant::now();
    let limit = parser_timeout();
    let mut out: Vec<String> = vec![];
    for n in sheets {
        if limit > 0 && t0.elapsed().as_secs() >= limit {
            break;
        }
        let data = match zip_read(&mut z, &n) {
            Some(d) => d,
            None => continue,
        };
        let root = match xmldom::parse(&data) {
            Ok(r) => r,
            Err(_) => continue,
        };
        for row in root.iter() {
            if row.tag != "row" {
                continue;
            }
            let cells: Vec<String> = row.children.iter().filter(|c| c.tag == "c")
                .map(|c| xlsx_cell(c, &shared, &styles)).collect();
            out.push(cells.join("\t"));
        }
    }
    // 메모(주석·스레드 댓글)
    let mut picked: Vec<(String, bool)> = vec![];
    for n in &names {
        if re_c.is_match(n) {
            picked.push((n.clone(), false));
        } else if re_tc.is_match(n) {
            picked.push((n.clone(), true));
        }
    }
    picked.sort_by_key(|(n, t)| num_key(n, if *t { &re_tc } else { &re_c }));
    for (n, threaded) in picked {
        let data = match zip_read(&mut z, &n) {
            Some(d) => d,
            None => continue,
        };
        let root = match xmldom::parse(&data) {
            Ok(r) => r,
            Err(_) => continue,
        };
        let (outer, inner) = if threaded { ("threadedComment", "text") } else { ("comment", "t") };
        for c in root.iter() {
            if c.tag != outer {
                continue;
            }
            let txt: String = c.iter().into_iter().filter(|e| e.tag == inner).map(|e| e.text.as_str()).collect();
            let t = pyutil::strip(&txt);
            if !t.is_empty() {
                out.push(t.to_string());
            }
        }
    }
    Ok(out.join("\n"))
}

// 공유 문자열 표
pub fn xlsx_shared(z: &mut zip::ZipArchive<std::fs::File>, names: &[String]) -> Vec<String> {
    if !names.iter().any(|n| n == "xl/sharedStrings.xml") {
        return vec![];
    }
    let data = match zip_read(z, "xl/sharedStrings.xml") {
        Some(d) => d,
        None => return vec![],
    };
    let root = match xmldom::parse(&data) {
        Ok(r) => r,
        Err(_) => return vec![],
    };
    root.children.iter().filter(|si| si.tag == "si")
        .map(|si| si.iter().into_iter().filter(|e| e.tag == "t").map(|e| e.text.as_str()).collect::<String>())
        .collect()
}

// 칸 서식 → 날짜 종류 목록(cellXfs 순서)
pub fn xlsx_date_styles(z: &mut zip::ZipArchive<std::fs::File>, names: &[String]) -> Vec<u8> {
    if !names.iter().any(|n| n == "xl/styles.xml") {
        return vec![];
    }
    let data = match zip_read(z, "xl/styles.xml") {
        Some(d) => d,
        None => return vec![],
    };
    let root = match xmldom::parse(&data) {
        Ok(r) => r,
        Err(_) => return vec![],
    };
    let mut custom = std::collections::HashMap::new();
    for e in root.iter() {
        if e.tag != "numFmt" {
            continue;
        }
        if let Some(id) = e.get("numFmtId").and_then(|v| v.trim().parse::<i64>().ok()) {
            custom.insert(id, crate::xlsdate::code_kind(e.get("formatCode").unwrap_or("")));
        }
    }
    let mut out = vec![];
    if let Some(grp) = root.iter().into_iter().find(|g| g.tag == "cellXfs") {
        for xf in grp.children.iter().filter(|x| x.tag == "xf") {
            let fid = xf.get("numFmtId").and_then(|v| v.trim().parse::<i64>().ok()).unwrap_or(0);
            out.push(*custom.get(&fid).unwrap_or(&crate::xlsdate::builtin_kind(fid)));
        }
    }
    out
}

// 셀 하나의 값
pub fn xlsx_cell(c: &xmldom::Node, shared: &[String], styles: &[u8]) -> String {
    let t = c.get("t");
    if t == Some("s") {
        for e in &c.children {
            if e.tag == "v" {
                return pyutil::strip(&e.text).parse::<usize>().ok().and_then(|i| shared.get(i).cloned()).unwrap_or_default();
            }
        }
        return String::new();
    }
    if t == Some("inlineStr") {
        return c.iter().into_iter().filter(|e| e.tag == "t").map(|e| e.text.as_str()).collect();
    }
    for e in &c.children {
        if e.tag == "v" {
            let raw = e.text.clone();
            if (t.is_none() || t == Some("n")) && !styles.is_empty() {
                let kind = c.get("s").and_then(|s| s.trim().parse::<usize>().ok()).and_then(|i| styles.get(i).copied())
                    .unwrap_or(crate::xlsdate::KIND_NONE);
                if let Some(shown) = crate::xlsdate::serial_to_string(&raw, kind) {
                    return shown;
                }
            }
            return raw;
        }
    }
    String::new()
}

// PPTX 슬라이드·노트 문단
fn pptx_text(path: &str) -> Result<String, String> {
    let re_s = regex::Regex::new(r"(?i)ppt/slides/slide(\d+)\.xml$").unwrap();
    let re_n = regex::Regex::new(r"(?i)ppt/notesSlides/notesSlide(\d+)\.xml$").unwrap();
    let mut z = zip_open(path, "pptx")?;
    let names: Vec<String> = z.file_names().map(|s| s.to_string()).collect();
    let mut slides: Vec<String> = names.iter().filter(|n| re_s.is_match(n)).cloned().collect();
    slides.sort_by_key(|n| num_key(n, &re_s));
    let mut notes: Vec<String> = names.iter().filter(|n| re_n.is_match(n)).cloned().collect();
    notes.sort_by_key(|n| num_key(n, &re_n));
    let mut out = vec![];
    for n in slides.into_iter().chain(notes) {
        let data = match zip_read(&mut z, &n) {
            Some(d) => d,
            None => continue,
        };
        if let Ok(root) = xmldom::parse(&data) {
            for p in root.iter() {
                if p.tag != "p" {
                    continue;
                }
                let txt: String = p.iter().into_iter().filter(|e| e.tag == "t").map(|e| e.text.as_str()).collect();
                if !txt.is_empty() {
                    out.push(txt);
                }
            }
        }
    }
    Ok(out.join("\n"))
}

// HWPX 문단 (정규식 토막 — 기존 hwpx_zip.py 와 같은 방식)
fn hwpx_text(path: &str) -> Result<String, String> {
    let re_sec = regex::Regex::new(r"(?i)Contents/section\d+\.xml$").unwrap();
    let re_no = regex::Regex::new(r"(?i)section(\d+)\.xml$").unwrap();
    let tok = regex::Regex::new(
        r"(?s)<hp:p(?:\s[^>]*?)?/>|<hp:p(?:\s[^>]*)?>|</hp:p>|<hp:t(?:\s[^>]*)?>(.*?)</hp:t>|<hp:lineBreak\b[^>]*>|<hp:tab\b[^>]*>").unwrap();
    let inner = regex::Regex::new(r"<[^>]+>").unwrap();
    let mut z = zip_open(path, "HWPX(zip)")?;
    let mut names: Vec<String> = z.file_names().filter(|n| re_sec.is_match(n)).map(|s| s.to_string()).collect();
    names.sort_by_key(|n| num_key(n, &re_no));
    let mut parts: Vec<String> = vec![];
    for n in names {
        let data = zip_read(&mut z, &n).unwrap_or_default();
        let xml = String::from_utf8_lossy(&data);
        let mut line = String::new();
        let flush = |line: &mut String, parts: &mut Vec<String>| {
            if !pyutil::strip(line).is_empty() {
                parts.push(pyutil::rstrip(line).to_string());
            }
            line.clear();
        };
        for m in tok.captures_iter(&xml) {
            if let Some(run) = m.get(1) {
                line.push_str(&inner.replace_all(run.as_str(), ""));
                continue;
            }
            let whole = m.get(0).unwrap().as_str();
            if whole.starts_with("<hp:lineBreak") {
                line.push('\n');
            } else if whole.starts_with("<hp:tab") {
                line.push('\t');
            } else {
                flush(&mut line, &mut parts);
            }
        }
        flush(&mut line, &mut parts);
    }
    let mut text = parts.join("\n");
    for (a, b) in [("&lt;", "<"), ("&gt;", ">"), ("&quot;", "\""), ("&apos;", "'"), ("&amp;", "&")] {
        text = text.replace(a, b);
    }
    Ok(text)
}

//------------------------------------------------------------------
// 사이냅 문서 필터(snf_exe) 폴백
//=> 실행 파일이 있을 때만 쓴다(SIMPLERAG_SNF_EXE 또는 exe 옆 synap\snf_exe.exe).
//------------------------------------------------------------------
fn snf_path() -> Option<String> {
    if let Some(p) = &crate::config::cfg().cso_snf {
        if std::path::Path::new(p).is_file() {
            return Some(p.clone());
        }
    }
    let d = pyutil::exe_dir();
    for c in [d.join("synap").join("windows").join("snf_exe.exe"), d.join("synap").join("snf_exe.exe")] {
        if c.is_file() {
            return Some(c.to_string_lossy().into_owned());
        }
    }
    None
}

fn snf_text(exe: &str, path: &str) -> Result<String, String> {
    let out_path = std::env::temp_dir().join(format!("csoclassify_{}.txt", std::process::id() as u64 * 1_000_003 + pyutil::now_ts() as u64));
    let mut cmd = std::process::Command::new(exe);
    cmd.args(["-U8", "-NO_WITHPAGE", path]).arg(&out_path);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd.stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
        .spawn().map_err(|e| format!("snf 실행 실패: {}", e))?;
    let t0 = std::time::Instant::now();
    let timeout = std::env::var("CSOCLASSIFY_SNF_TIMEOUT").ok().and_then(|v| v.parse().ok()).unwrap_or(60u64);
    loop {
        if let Ok(Some(_)) = child.try_wait() {
            break;
        }
        if t0.elapsed().as_secs() >= timeout {
            let _ = child.kill();
            let _ = std::fs::remove_file(&out_path);
            return Err(format!("snf_exe 타임아웃({}s)", timeout));
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    let data = std::fs::read(&out_path).unwrap_or_default();
    let _ = std::fs::remove_file(&out_path);
    if data.is_empty() {
        return Err("출력파일 미생성".into());
    }
    Ok(String::from_utf8_lossy(&data).into_owned())
}

// io 오류 종류 이름(파이썬 예외 이름 흉내)
fn io_kind(e: &std::io::Error) -> &'static str {
    match e.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        _ => "OSError",
    }
}

// 형식 이름 → 엔진
fn run_engine(ftype: &str, path: &str) -> Option<Result<String, String>> {
    Some(match ftype {
        "pdf" => pdf_text(path),
        "hwp" => hwp5_text(path),
        "hwpx" => hwpx_text(path),
        "doc" => doc_text(path),
        "xls" => xls_text(path),
        "ppt" => ppt_text(path),
        "docx" => docx_text(path),
        "xlsx" => xlsx_text(path),
        "pptx" => pptx_text(path),
        "html" => html_text(path),
        "text" => plaintext(path),
        _ => return None,
    })
}

//------------------------------------------------------------------
// 하이브리드 추출 (HybridExtractor.extract) — 진입점
//
// -in: path = 파일 경로
//
// -out: Ok(본문) / Err(실패 사유 — "하이브리드 전엔진 실패(...)" 등)
// -out: error = 크기 상한 초과·모든 엔진 실패면 Err
//------------------------------------------------------------------
pub fn extract(path: &str) -> Result<String, String> {
    let ftype = detect(path);
    // 크기 상한
    if let Ok(m) = std::fs::metadata(path) {
        let (limit, which) = if ftype == "text" || ftype == "html" {
            (max_file_bytes_text(), "MAX_FILE_BYTES_TEXT")
        } else {
            (max_file_bytes(), "MAX_FILE_BYTES")
        };
        if limit > 0 && m.len() > limit {
            return Err(format!("크기 상한 초과: {} > {} ({})", human_bytes(m.len()), human_bytes(limit), which));
        }
    }
    let snf = snf_path();
    let mut chain: Vec<&str> = vec![];
    if ["pdf", "hwp", "hwpx", "doc", "xls", "ppt", "docx", "xlsx", "pptx", "html", "text"].contains(&ftype) {
        chain.push(ftype);
    }
    if snf.is_some() {
        chain.push("snf");
    }
    if chain.is_empty() {
        return Err(format!("사용 가능한 추출기 없음(감지={})", ftype));
    }
    let mut reasons = vec![];
    let mut best_short: Option<String> = None;
    for name in chain {
        let r = if name == "snf" { snf_text(snf.as_deref().unwrap(), path) } else { run_engine(name, path).unwrap() };
        let text = match r {
            Ok(t) => t,
            Err(e) => {
                reasons.push(format!("{}:{}", name, e));
                continue;
            }
        };
        if pyutil::strip(&text).is_empty() {
            reasons.push(format!("{}:empty", name));
            continue;
        }
        let n_chars = text.chars().filter(|c| !pyutil::is_space(*c)).count();
        if n_chars < min_text_len() {
            reasons.push(format!("{}:short({}자)", name, n_chars));
            if best_short.as_ref().map(|b| pyutil::clen(&text) > pyutil::clen(b)).unwrap_or(true) {
                best_short = Some(text);
            }
            continue;
        }
        return Ok(text);
    }
    if let Some(t) = best_short {
        return Ok(t);
    }
    Err(format!("하이브리드 전엔진 실패(감지={}; {})", ftype, reasons.join("; ")))
}

// 사람이 읽는 크기
fn human_bytes(n: u64) -> String {
    let f = n as f64;
    if f >= 1048576.0 {
        format!("{:.1}MB", f / 1048576.0)
    } else if f >= 1024.0 {
        format!("{:.1}KB", f / 1024.0)
    } else {
        format!("{}B", n)
    }
}
