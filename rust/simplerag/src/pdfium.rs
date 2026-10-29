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
}

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
pub struct PageText {
    pub text: String,
    pub n_chars: i32,
    pub sizes: Option<Vec<f64>>,
}

//------------------------------------------------------------------
// PDF 를 쪽별 글자로 읽기 (핵심)
//    1) 파일을 메모리로 읽어 FPDF_LoadMemDocument64 — 경로 인코딩 문제가 없다
//    2) 쪽마다 텍스트 페이지를 열어 get_text_range 와 같은 방식으로 글자를 꺼낸다
//    3) on_page 가 false 를 돌려주면(시간·쪽 상한) 그만 읽는다
//
// -in: path       = PDF 경로
// -in: want_sizes = 글꼴 크기도 읽을지
// -in: on_page    = 쪽 번호(0부터), 전체 쪽 수 → 계속 읽을지
//
// -out: Ok((쪽 결과 목록, 전체 쪽 수))
// -out: error = DLL·파일·열기 실패면 Err(오류문)
//------------------------------------------------------------------
pub fn read_pages(path: &str, want_sizes: bool, mut on_page: impl FnMut(usize, usize) -> bool)
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
                out.push(PageText { text: String::new(), n_chars: 0, sizes: None });
                continue;
            }
            let tp = (a.text_load)(page);
            let mut pt = PageText { text: String::new(), n_chars: 0, sizes: None };
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
