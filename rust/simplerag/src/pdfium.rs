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
// pdfium 을 쓸 수 있는가 (status·진단용)
//------------------------------------------------------------------
pub fn available() -> Result<(), String> {
    api().map(|_| ())
}
