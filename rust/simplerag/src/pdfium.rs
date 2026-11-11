//------------------------------------------------------------------
// pdfium.dll 직접 호출 (pypdfium2 대체)
//=> Python 판은 pypdfium2 의 PdfTextPage.get_text_range() 로 쪽 글자를 읽는다. 같은 글자가
//   나오도록 같은 pdfium API(FPDFText_GetText + 앞뒤 '제외 글자' 보정)를 그대로 부른다.
//   DLL 은 exe 옆 pdfium.dll(파이썬 배포본 pypdfium2_raw 의 것과 같은 파일)을 쓴다.
//   pdfium 은 스레드 안전하지 않으므로 전역 잠금 하나로 모든 호출을 줄 세운다.
//------------------------------------------------------------------

use std::ffi::c_void;
use std::sync::{Mutex, OnceLock};

use libloading::{Library, Symbol};

type Doc = *mut c_void;
type Page = *mut c_void;
type TextPage = *mut c_void;

// 쓰는 함수 묶음 — 적재 때 한 번에 찾아 둔다
struct Api {
    _lib: Library,
    load_mem: unsafe extern "C" fn(*const c_void, usize, *const i8) -> Doc,
    close_doc: unsafe extern "C" fn(Doc),
    page_count: unsafe extern "C" fn(Doc) -> i32,
    load_page: unsafe extern "C" fn(Doc, i32) -> Page,
    close_page: unsafe extern "C" fn(Page),
    text_load: unsafe extern "C" fn(Page) -> TextPage,
    text_close: unsafe extern "C" fn(TextPage),
    count_chars: unsafe extern "C" fn(TextPage) -> i32,
    get_text: unsafe extern "C" fn(TextPage, i32, i32, *mut u16) -> i32,
    text_index_from_char: unsafe extern "C" fn(TextPage, i32) -> i32,
    font_size: unsafe extern "C" fn(TextPage, i32) -> f64,
    // 메타데이터 · 책갈피 · 오류 코드 (meta::extract 의 pdf_meta 용)
    get_meta_text: unsafe extern "C" fn(Doc, *const i8, *mut c_void, u32) -> u32,
    bm_first_child: unsafe extern "C" fn(Doc, *mut c_void) -> *mut c_void,
    bm_next_sibling: unsafe extern "C" fn(Doc, *mut c_void) -> *mut c_void,
    last_error: unsafe extern "C" fn() -> u32,
    // 표 검출(docmodel::pdftable) 용 — 경로 객체 · 글자 위치
    page_count_objects: unsafe extern "C" fn(Page) -> i32,
    page_get_object: unsafe extern "C" fn(Page, i32) -> *mut c_void,
    obj_get_type: unsafe extern "C" fn(*mut c_void) -> i32,
    obj_get_bounds: unsafe extern "C" fn(*mut c_void, *mut f32, *mut f32, *mut f32, *mut f32) -> i32,
    form_count_objects: unsafe extern "C" fn(*mut c_void) -> i32,
    form_get_object: unsafe extern "C" fn(*mut c_void, u32) -> *mut c_void,
    get_unicode: unsafe extern "C" fn(TextPage, i32) -> u32,
    get_char_box: unsafe extern "C" fn(TextPage, i32, *mut f64, *mut f64, *mut f64, *mut f64) -> i32,
    get_char_origin: unsafe extern "C" fn(TextPage, i32, *mut f64, *mut f64) -> i32,
}

// pdfium 쪽 객체 종류 번호 (fpdf_edit.h)
const PAGEOBJ_PATH: i32 = 2;
const PAGEOBJ_FORM: i32 = 5;
// pypdfium2 get_objects(max_depth=5) 와 같게 — 폼 안 폼은 4단계까지 들어간다
const FORM_MAX_DEPTH: usize = 5;

// Api 는 잠금 안에서만 쓰므로 스레드 사이로 옮겨도 안전하다
unsafe impl Send for Api {}

static API: OnceLock<Result<Mutex<Api>, String>> = OnceLock::new();

//------------------------------------------------------------------
// pdfium.dll 경로 찾기
//=> SIMPLERAG_PDFIUM 환경변수 → exe 옆 → exe 옆 _internal\pypdfium2_raw (파이썬 배포본 모양)
//------------------------------------------------------------------
fn dll_path() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("SIMPLERAG_PDFIUM") {
        return p.into();
    }
    let dir = crate::pyutil::exe_dir();
    let a = dir.join("pdfium.dll");
    if a.is_file() {
        return a;
    }
    dir.join("_internal").join("pypdfium2_raw").join("pdfium.dll")
}

// DLL 적재 + 함수 찾기 + 라이브러리 초기화
fn load() -> Result<Mutex<Api>, String> {
    let path = dll_path();
    unsafe {
        let lib = Library::new(&path).map_err(|e| format!("pdfium.dll 적재 실패({}): {}", path.display(), e))?;
        macro_rules! sym {
            ($name:expr) => {{
                let s: Symbol<*const c_void> = lib.get($name).map_err(|e| format!("pdfium 함수 없음: {}", e))?;
                std::mem::transmute(*s)
            }};
        }
        let init: unsafe extern "C" fn() = sym!(b"FPDF_InitLibrary\0");
        init();
        let api = Api {
            load_mem: sym!(b"FPDF_LoadMemDocument64\0"),
            close_doc: sym!(b"FPDF_CloseDocument\0"),
            page_count: sym!(b"FPDF_GetPageCount\0"),
            load_page: sym!(b"FPDF_LoadPage\0"),
            close_page: sym!(b"FPDF_ClosePage\0"),
            text_load: sym!(b"FPDFText_LoadPage\0"),
            text_close: sym!(b"FPDFText_ClosePage\0"),
            count_chars: sym!(b"FPDFText_CountChars\0"),
            get_text: sym!(b"FPDFText_GetText\0"),
            text_index_from_char: sym!(b"FPDFText_GetTextIndexFromCharIndex\0"),
            font_size: sym!(b"FPDFText_GetFontSize\0"),
            get_meta_text: sym!(b"FPDF_GetMetaText\0"),
            bm_first_child: sym!(b"FPDFBookmark_GetFirstChild\0"),
            bm_next_sibling: sym!(b"FPDFBookmark_GetNextSibling\0"),
            last_error: sym!(b"FPDF_GetLastError\0"),
            page_count_objects: sym!(b"FPDFPage_CountObjects\0"),
            page_get_object: sym!(b"FPDFPage_GetObject\0"),
            obj_get_type: sym!(b"FPDFPageObj_GetType\0"),
            obj_get_bounds: sym!(b"FPDFPageObj_GetBounds\0"),
            form_count_objects: sym!(b"FPDFFormObj_CountObjects\0"),
            form_get_object: sym!(b"FPDFFormObj_GetObject\0"),
            get_unicode: sym!(b"FPDFText_GetUnicode\0"),
            get_char_box: sym!(b"FPDFText_GetCharBox\0"),
            get_char_origin: sym!(b"FPDFText_GetCharOrigin\0"),
            _lib: lib,
        };
        Ok(Mutex::new(api))
    }
}

fn api() -> Result<&'static Mutex<Api>, String> {
    match API.get_or_init(load) {
        Ok(m) => Ok(m),
        Err(e) => Err(e.clone()),
    }
}

//------------------------------------------------------------------
// 쪽 하나의 읽은 결과
// -필드: text  = get_text_range() 와 같은 글자
// -필드: sizes = 글자 위치별 글꼴 크기(count_chars 와 text 길이가 같을 때만 채운다, P-2 용)
//------------------------------------------------------------------
// -필드: tables = 테두리 표(want_tables 일 때만, 위 → 아래). used 는 text 안 글자 위치
pub struct PageText {
    pub text: String,
    pub n_chars: i32,
    pub sizes: Option<Vec<f64>>,
    pub tables: Vec<crate::docmodel::pdftable::Table>,
}

//------------------------------------------------------------------
// PDF 를 쪽별 글자로 읽기 (핵심)
//    1) 파일을 메모리로 읽어 FPDF_LoadMemDocument64 — 경로 인코딩 문제가 없다
//    2) 쪽마다 텍스트 페이지를 열어 get_text_range 와 같은 방식으로 글자를 꺼낸다
//    3) on_page 가 false 를 돌려주면(시간·쪽 상한) 그만 읽는다
//
// -in: path       = PDF 경로
// -in: want_sizes = 글꼴 크기도 읽을지
// -in: want_tables = 테두리 표도 찾을지(docmodel::pdftable)
// -in: on_page    = 쪽 번호(0부터), 전체 쪽 수 → 계속 읽을지
//
// -out: Ok((쪽 결과 목록, 전체 쪽 수))
// -out: error = DLL·파일·열기 실패면 Err(오류문)
//------------------------------------------------------------------
pub fn read_pages(path: &str, want_sizes: bool, want_tables: bool, mut on_page: impl FnMut(usize, usize) -> bool)
                  -> Result<(Vec<PageText>, usize), String> {
    let data = std::fs::read(path).map_err(|e| format!("PDF 읽기 실패: {}", e))?;
    let m = api()?;
    let a = m.lock().unwrap_or_else(|p| p.into_inner());
    unsafe {
        let doc = (a.load_mem)(data.as_ptr() as *const c_void, data.len(), std::ptr::null());
        if doc.is_null() {
            return Err("PdfiumError".into());
        }
        let n = (a.page_count)(doc).max(0) as usize;
        let mut out = vec![];
        for i in 0..n {
            if !on_page(i, n) {
                break;
            }
            let page = (a.load_page)(doc, i as i32);
            if page.is_null() {
                out.push(PageText { text: String::new(), n_chars: 0, sizes: None, tables: vec![] });
                continue;
            }
            let tp = (a.text_load)(page);
            let mut pt = PageText { text: String::new(), n_chars: 0, sizes: None, tables: vec![] };
            if !tp.is_null() {
                pt.n_chars = (a.count_chars)(tp);
                pt.text = text_range(&a, tp, pt.n_chars);
                if want_sizes {
                    let mut v = Vec::with_capacity(pt.n_chars.max(0) as usize);
                    for k in 0..pt.n_chars.max(0) {
                        v.push((a.font_size)(tp, k));
                    }
                    pt.sizes = Some(v);
                }
                // 표 검출 실패는 본문 추출을 막지 않는다 — 빈 목록이면 기존처럼 줄만 남는다
                if want_tables && !pt.text.is_empty() {
                    pt.tables = page_tables(&a, page, tp, pt.n_chars, &pt.text);
                }
                (a.text_close)(tp);
            }
            (a.close_page)(page);
            out.push(pt);
        }
        (a.close_doc)(doc);
        Ok((out, n))
    }
}

//------------------------------------------------------------------
// 쪽 하나의 테두리 표 찾기 (Python pdftable.find_tables)
//=> 선이 없는 쪽은 선만 세고 바로 끝난다(대부분의 본문 쪽). 선이 서로 닿는 묶음이 있을 때만
//   글자 위치를 읽어 표를 만든다.
// -out: 표 목록 — pdfium 호출 실패·글자 위치 불일치면 빈 목록
//------------------------------------------------------------------
unsafe fn page_tables(a: &Api, page: Page, tp: TextPage, n_chars: i32, text: &str)
                      -> Vec<crate::docmodel::pdftable::Table> {
    use crate::docmodel::pdftable as T;
    let (mut h, mut v) = (vec![], vec![]);
    let mut n = 0usize;
    match walk_paths(a, page, false, 0, &mut h, &mut v, &mut n) {
        Ok(true) => {}
        _ => return vec![], // 실패했거나 경로가 너무 많다(그림 쪽)
    }
    if h.len() < 2 || v.len() < 2 {
        return vec![];
    }
    let comps = T::components(&h, &v);
    if comps.is_empty() {
        return vec![];
    }
    match page_chars(a, tp, n_chars, text) {
        Some(chars) => T::tables_from(&comps, &chars),
        None => vec![],
    }
}

//------------------------------------------------------------------
// 쪽(또는 폼)의 경로 객체를 선으로 모으기 (Python pdftable._segments)
//=> pypdfium2 get_objects 와 같은 순서(앞 순회, 폼 안으로 내려감)로 경로만 본다.
//   얇고 긴 것은 선, 사각형은 네 변을 선으로 넣는다(셀 테두리를 사각형으로 그리는 문서가 있다).
// -out: Ok(true) = 다 읽음 / Ok(false) = 경로가 MAX_PATHS 를 넘음 / Err = pdfium 실패
//------------------------------------------------------------------
#[allow(clippy::too_many_arguments)]
unsafe fn walk_paths(a: &Api, parent: *mut c_void, is_form: bool, level: usize,
                     h: &mut Vec<(f64, f64, f64)>, v: &mut Vec<(f64, f64, f64)>, n: &mut usize) -> Result<bool, ()> {
    use crate::docmodel::pdftable::{MAX_PATHS, MIN_SEG, THIN};
    let count = if is_form { (a.form_count_objects)(parent) } else { (a.page_count_objects)(parent) };
    if count < 0 {
        return Err(());
    }
    for i in 0..count {
        let obj = if is_form { (a.form_get_object)(parent, i as u32) } else { (a.page_get_object)(parent, i) };
        if obj.is_null() {
            return Err(());
        }
        let kind = (a.obj_get_type)(obj);
        if kind == PAGEOBJ_PATH {
            *n += 1;
            if *n > MAX_PATHS {
                return Ok(false);
            }
            let (mut l, mut b, mut r, mut t) = (0f32, 0f32, 0f32, 0f32);
            if (a.obj_get_bounds)(obj, &mut l, &mut b, &mut r, &mut t) == 0 {
                return Err(());
            }
            // pypdfium2 는 c_float 를 파이썬 float 로 바꿔 계산한다 — f64 로 넓힌 뒤 계산
            let (l, b, r, t) = (l as f64, b as f64, r as f64, t as f64);
            let (w, ht) = (r - l, t - b);
            if ht < THIN && w > MIN_SEG {
                h.push(((b + t) / 2.0, l, r));
            } else if w < THIN && ht > MIN_SEG {
                v.push(((l + r) / 2.0, b, t));
            } else if w >= THIN && ht >= THIN {
                h.push((b, l, r));
                h.push((t, l, r));
                v.push((l, b, t));
                v.push((r, b, t));
            }
        }
        if kind == PAGEOBJ_FORM && level < FORM_MAX_DEPTH - 1 && !walk_paths(a, obj, true, level + 1, h, v, n)? {
            return Ok(false);
        }
    }
    Ok(true)
}

//------------------------------------------------------------------
// 쪽 글자 위치 읽기 (Python pdftable._page_chars)
//=> pdfium 글자 번호와 쪽 글(text)의 글자 위치를 맞춰 가며 좌표를 읽는다. pdfium 은 UTF-16
//   단위로 세서 기본 평면 밖 글자는 두 번호(상·하위 서로게이트)를 차지하지만 글에서는 한
//   글자다 — 하위 번호는 건너뛴다. 끝까지 맞춰 봐서 길이가 다르면 위치를 믿을 수 없어 None.
// -out: 글자 위치 목록(줄바꿈 제외) / 맞출 수 없거나 글자 상자 실패면 None
//------------------------------------------------------------------
unsafe fn page_chars(a: &Api, tp: TextPage, n: i32, text: &str) -> Option<Vec<crate::docmodel::pdftable::CharPos>> {
    let txt: Vec<char> = text.chars().collect();
    let mut out = vec![];
    let (mut i, mut pos) = (0i32, 0usize);
    while i < n {
        if pos >= txt.len() {
            return None;
        }
        let u = (a.get_unicode)(tp, i);
        let ch = txt[pos];
        if ch != '\r' && ch != '\n' {
            let (mut l, mut r, mut b, mut t) = (0f64, 0f64, 0f64, 0f64);
            if (a.get_char_box)(tp, i, &mut l, &mut r, &mut b, &mut t) == 0 {
                return None;
            }
            let (mut x, mut y) = (0f64, 0f64);
            (a.get_char_origin)(tp, i, &mut x, &mut y);
            out.push(crate::docmodel::pdftable::CharPos { ch, cx: (l + r) / 2.0, y, pos });
        }
        // 상위 서로게이트면 다음 번호(하위)는 같은 글자 — 건너뛴다
        i += if (0xD800..0xDC00).contains(&u) { 2 } else { 1 };
        pos += 1;
    }
    if pos == txt.len() { Some(out) } else { None }
}

//------------------------------------------------------------------
// get_text_range(index=0, count=-1) 그대로
//=> 앞뒤에 '텍스트에 안 나오는 글자'가 있으면 범위를 줄인 뒤 FPDFText_GetText 로 읽고
//   UTF-16LE 를 errors="ignore" 로 푼다(짝 없는 서로게이트는 버린다).
//------------------------------------------------------------------
unsafe fn text_range(a: &Api, tp: TextPage, count_all: i32) -> String {
    if count_all <= 0 {
        return String::new();
    }
    let (mut c_start, mut c_end) = (0i32, count_all - 1);
    let (mut l_passive, mut r_passive) = (0i32, 0i32);
    let (t_start, t_end);
    loop {
        if c_start > c_end {
            return String::new();
        }
        let ts = (a.text_index_from_char)(tp, c_start);
        if ts == -1 {
            c_start += 1;
            l_passive += 1;
            continue;
        }
        let te = (a.text_index_from_char)(tp, c_end);
        if te == -1 {
            c_end -= 1;
            r_passive += 1;
            continue;
        }
        t_start = ts;
        t_end = te;
        break;
    }
    let index = l_passive;
    let count = count_all - l_passive - r_passive;
    let in_count = (t_end + 2 - t_start).max(1) as usize;
    let mut buf = vec![0u16; in_count];
    let out_count = (a.get_text)(tp, index, count, buf.as_mut_ptr());
    let used = if out_count > 0 { (out_count as usize - 1).min(buf.len()) } else { 0 };
    char::decode_utf16(buf[..used].iter().copied()).filter_map(|r| r.ok()).collect()
}

//------------------------------------------------------------------
// PDF 속성 묶음 (meta::extract::pdf_meta 용 — pypdfium2 로 읽던 값과 같다)
// -필드: info       = Info 사전 (키, 값) — 값이 빈 것은 뺀다(get_metadata_dict(skip_empty=True))
// -필드: pages      = 쪽 수 / toc = 책갈피 수(깊이 2까지, get_toc(max_depth=2))
// -필드: text_chars = 앞 3쪽 글자 중 공백이 아닌 글자 수(스캔 판정)
//------------------------------------------------------------------
pub struct PdfMeta {
    pub info: Vec<(String, String)>,
    pub pages: i64,
    pub toc: i64,
    pub text_chars: i64,
}

// pypdfium2 ErrorToStr 와 같은 오류 이름
fn err_str(code: u32) -> &'static str {
    match code {
        0 => "Success",
        1 => "Unknown error",
        2 => "File access error",
        3 => "Data format error",
        4 => "Incorrect password error",
        5 => "Unsupported security scheme error",
        6 => "Page not found or content error",
        _ => "Unknown error",
    }
}

//------------------------------------------------------------------
// PDF 속성 읽기 — Info 사전 · 쪽 수 · 책갈피 수 · 앞 3쪽 글자 수
//=> pypdfium2 판과 같은 순서 · 같은 실패 문구로 읽는다.
//    1) 문서 열기 — 쪽 수가 1 미만이면 "Failed to load document (PDFium: …)."
//    2) Title · Author · Subject · Keywords · Creator · Producer · CreationDate · ModDate 중
//       Python 이 쓰는 여섯 개만 FPDF_GetMetaText 로(UTF-16LE 엄격 풀기 — 틀리면 실패)
//    3) 책갈피 — 같은 책갈피를 다시 만나면 멈춘다(순환 참조), 깊이 0 · 1 만 센다
//    4) 앞 3쪽 get_text_range() 에서 공백(파이썬 \s)을 뺀 글자 수
//
// -in: path = PDF 경로
//
// -out: Ok(PdfMeta)
// -out: error = DLL 적재 실패 · 열기 실패 · 쪽 열기 실패면 Err("PdfiumError: …" 꼴)
//------------------------------------------------------------------
pub fn meta_info(path: &str) -> Result<PdfMeta, String> {
    let data = std::fs::read(path).map_err(|e| format!("OSError: {}", e))?;
    let m = api()?;
    let a = m.lock().unwrap_or_else(|p| p.into_inner());
    unsafe {
        let doc = (a.load_mem)(data.as_ptr() as *const c_void, data.len(), std::ptr::null());
        if doc.is_null() || (a.page_count)(doc) < 1 {
            let code = (a.last_error)();
            if !doc.is_null() {
                (a.close_doc)(doc);
            }
            return Err(format!("PdfiumError: Failed to load document (PDFium: {}).", err_str(code)));
        }
        let res = (|| -> Result<PdfMeta, String> {
            let mut info = vec![];
            for key in ["Title", "Author", "Creator", "Producer", "CreationDate", "ModDate"] {
                let k = format!("{}\0", key);
                let n = (a.get_meta_text)(doc, k.as_ptr() as *const i8, std::ptr::null_mut(), 0);
                if n <= 2 {
                    continue;
                }
                let mut buf = vec![0u8; n as usize];
                (a.get_meta_text)(doc, k.as_ptr() as *const i8, buf.as_mut_ptr() as *mut c_void, n);
                let units: Vec<u16> = buf[..n as usize - 2].chunks(2)
                    .map(|c| u16::from_le_bytes([c[0], *c.get(1).unwrap_or(&0)])).collect();
                if (n as usize - 2) % 2 != 0 {
                    return Err("UnicodeDecodeError: 'utf-16-le' codec can't decode byte: truncated data".into());
                }
                let s: String = char::decode_utf16(units.iter().copied()).collect::<Result<String, _>>()
                    .map_err(|_| "UnicodeDecodeError: 'utf-16-le' codec can't decode bytes: illegal UTF-16 surrogate".to_string())?;
                if !s.is_empty() {
                    info.push((key.to_string(), s));
                }
            }
            let pages = (a.page_count)(doc) as i64;
            // 책갈피 수 — get_toc(max_depth=2)
            let mut seen: std::collections::HashSet<usize> = std::collections::HashSet::new();
            fn walk(a: &Api, doc: Doc, parent: *mut c_void, level: i32, seen: &mut std::collections::HashSet<usize>) -> i64 {
                let mut n = 0;
                let mut bm = unsafe { (a.bm_first_child)(doc, parent) };
                while !bm.is_null() {
                    if !seen.insert(bm as usize) {
                        break;
                    }
                    n += 1;
                    if level < 1 {
                        n += walk(a, doc, bm, level + 1, seen);
                    }
                    bm = unsafe { (a.bm_next_sibling)(doc, bm) };
                }
                n
            }
            let toc = walk(&a, doc, std::ptr::null_mut(), 0, &mut seen);
            let mut chars = 0i64;
            for i in 0..pages.min(3) {
                let page = (a.load_page)(doc, i as i32);
                if page.is_null() {
                    return Err("PdfiumError: Failed to load page.".into());
                }
                let tp = (a.text_load)(page);
                if tp.is_null() {
                    (a.close_page)(page);
                    return Err("PdfiumError: Failed to load text page.".into());
                }
                let n = (a.count_chars)(tp);
                let t = text_range(&a, tp, n);
                // 공백만 있는 글자층은 스캔본과 같으니 공백을 빼고 센다
                chars += t.chars().filter(|c| !crate::pyutil::is_space(*c)).count() as i64;
                (a.text_close)(tp);
                (a.close_page)(page);
            }
            Ok(PdfMeta { info, pages, toc, text_chars: chars })
        })();
        (a.close_doc)(doc);
        res
    }
}

//------------------------------------------------------------------
// pdfium 을 쓸 수 있는가 (status·진단용)
//------------------------------------------------------------------
pub fn available() -> Result<(), String> {
    api().map(|_| ())
}
