//------------------------------------------------------------------
// 구조화 문서 파서 DLL 연결 (struct_docparse.dll — 설계서 2026-11-16 §10)
//=> 문서 → 블록 추출 · 스캔 OCR · PDF 쪽 글은 모두 이 DLL 이 한다(소스: D:\Project\Struct-DocParse-OCR-rust).
//   여기서는 DLL 을 한 번 적재해 함수 포인터를 쥐고, 결과 JSON 을 SimpleRAG 의 Document · Block 으로 바꾼다.
//    1) exe 옆(또는 SIMPLERAG_DOCPARSE) struct_docparse.dll 을 적재하고 ABI 버전을 확인한다
//    2) 설정(config.yaml extract.*)으로 옵션 JSON 을 만들어 sdp_open — OCR 은 항상 deferred(대기열은 색인이 돌린다)
//    3) 함수마다 상태 코드를 보고, 실패면 sdp_last_error 글을 그대로 오류로 돌려준다
//------------------------------------------------------------------

use std::ffi::{c_char, c_void, CStr, CString};
use std::sync::OnceLock;

use libloading::Library;
use serde_json::{json, Value};

use crate::config::cfg;
use crate::docmodel::blocks::{self as B, Block, Document};
use crate::pyutil;

// 이 SimpleRAG 가 아는 DLL 함수 모양 버전
const ABI_VERSION: u32 = 1;
// DLL 을 못 쓸 때 안내
const MISSING_HINT: &str = "문서 파서(struct_docparse.dll)를 불러오지 못했습니다 — 다시 설치해 주세요";

type FnAbi = unsafe extern "C" fn() -> u32;
type FnVersion = unsafe extern "C" fn() -> *const c_char;
type FnLog = Option<unsafe extern "C" fn(i32, *const c_char, *mut c_void)>;
type FnProgress = Option<unsafe extern "C" fn(i32, i32, f64, *mut c_void) -> i32>;
type FnOpen = unsafe extern "C" fn(*const c_char, FnLog, *mut c_void, *mut *mut c_void) -> i32;
type FnPathOut = unsafe extern "C" fn(*mut c_void, *const c_char, *mut *mut c_char) -> i32;
type FnPathOptOut = unsafe extern "C" fn(*mut c_void, *const c_char, *const c_char, *mut *mut c_char) -> i32;
type FnExtractText = unsafe extern "C" fn(*mut c_void, *const c_char, i32, *mut *mut c_char) -> i32;
type FnOcrPages = unsafe extern "C" fn(*mut c_void, *const c_char, *const c_char, FnProgress, *mut c_void, *mut *mut c_char) -> i32;
type FnStatus = unsafe extern "C" fn(*mut c_void, *mut *mut c_char) -> i32;
type FnSupports = unsafe extern "C" fn(*mut c_void, *const c_char) -> i32;
type FnSetOpt = unsafe extern "C" fn(*mut c_void, *const c_char, *const c_char) -> i32;
type FnHandle = unsafe extern "C" fn(*mut c_void);
type FnCacheRemove = unsafe extern "C" fn(*mut c_void, *const c_char) -> i32;
type FnLastErr = unsafe extern "C" fn() -> *const c_char;
type FnFree = unsafe extern "C" fn(*mut c_char);

//------------------------------------------------------------------
// 적재한 DLL
// -필드: _lib     = DLL (프로세스 끝까지 놓지 않는다 — 함수 포인터가 가리킨다)
// -필드: h        = sdp_open 이 준 핸들
// -필드: version  = "0.1.0 blocks=5 ocr=…"
// -필드: blocks_version = DLL 의 추출 결과 버전(재인덱싱 판정)
// -필드: 나머지   = DLL 함수
//------------------------------------------------------------------
struct Dll {
    _lib: Library,
    h: usize,
    version: String,
    blocks_version: i64,
    parse: FnPathOptOut,
    extract_text: FnExtractText,
    ocr_pages: FnOcrPages,
    ocr_file: FnPathOptOut,
    ocr_unload: FnHandle,
    ocr_cache_remove: FnCacheRemove,
    status: FnStatus,
    supports: FnSupports,
    set_option: FnSetOpt,
    pdf_info: FnPathOut,
    pdf_page_texts: FnPathOut,
    last_error: FnLastErr,
    free: FnFree,
}

static DLL: OnceLock<Result<Dll, String>> = OnceLock::new();

//------------------------------------------------------------------
// DLL 경로 — SIMPLERAG_DOCPARSE 환경변수 → exe 옆
//
// -in: 없음
//
// -out: 경로
// -out: error = 예외 없음
//------------------------------------------------------------------
fn dll_path() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("SIMPLERAG_DOCPARSE") {
        if !p.is_empty() {
            return p.into();
        }
    }
    pyutil::exe_dir().join("struct_docparse.dll")
}

// DLL 로그 → SimpleRAG 로그
unsafe extern "C" fn on_log(_level: i32, msg: *const c_char, _user: *mut c_void) {
    if !msg.is_null() {
        crate::cli::log(&CStr::from_ptr(msg).to_string_lossy());
    }
}

//------------------------------------------------------------------
// 설정 → sdp_open 옵션 JSON
//=> OCR 은 deferred — 추출은 캐시만 보고, 남은 쪽은 색인의 OCR 대기열(run_ocr_queue)이 sdp_ocr_pages 로 처리한다.
//   캐시는 지금과 같은 <데이터 폴더>\ocr_cache 라 옛 캐시를 그대로 쓴다.
//
// -in: 없음
//
// -out: 옵션 JSON
// -out: error = 예외 없음
//------------------------------------------------------------------
fn open_options() -> Value {
    let c = cfg();
    let mut runtime = json!({});
    if let Some(s) = &c.cso_snf {
        runtime["snf"] = json!(s);
    }
    json!({
        "ocr": {
            "enabled": c.ocr, "images": c.ocr_images, "mode": "deferred",
            "model_dir": c.ocr_dir, "cache_dir": pyutil::join(&c.root, "ocr_cache"),
            "threads": c.ocr_threads, "max_pages": c.ocr_max_pages,
        },
        "pdf": {"tables": c.pdf_tables, "infer_headings": c.pdf_infer_headings},
        "runtime": runtime,
    })
}

//------------------------------------------------------------------
// DLL 적재 + 함수 찾기 + 열기
//=> onnxruntime.dll 위치(ORT_DYLIB_PATH)를 먼저 정해 둔다 — DLL 의 OCR 도 같은 onnxruntime 을 쓴다.
//
// -in: 없음
//
// -out: Ok(Dll)
// -out: error = 파일 없음 · 함수 없음 · ABI 다름 · 열기 실패면 Err(안내문)
//------------------------------------------------------------------
fn load() -> Result<Dll, String> {
    crate::embed::ensure_dylib();
    let path = dll_path();
    unsafe {
        let lib = Library::new(&path).map_err(|e| format!("{} ({}: {})", MISSING_HINT, path.display(), e))?;
        macro_rules! sym {
            ($t:ty, $name:expr) => {{
                let s: libloading::Symbol<$t> = lib.get($name).map_err(|e| format!("{} (함수 없음: {})", MISSING_HINT, e))?;
                *s
            }};
        }
        let abi: FnAbi = sym!(FnAbi, b"sdp_abi_version\0");
        if abi() != ABI_VERSION {
            return Err(format!("{} (ABI {} ≠ {})", MISSING_HINT, abi(), ABI_VERSION));
        }
        let version_fn: FnVersion = sym!(FnVersion, b"sdp_version\0");
        let open: FnOpen = sym!(FnOpen, b"sdp_open\0");
        let last_error: FnLastErr = sym!(FnLastErr, b"sdp_last_error\0");
        let opts = CString::new(open_options().to_string()).unwrap_or_default();
        let mut h: *mut c_void = std::ptr::null_mut();
        let rc = open(opts.as_ptr(), Some(on_log), std::ptr::null_mut(), &mut h);
        if rc != 0 || h.is_null() {
            return Err(format!("문서 파서 열기 실패({}): {}", rc, CStr::from_ptr(last_error()).to_string_lossy()));
        }
        let version = CStr::from_ptr(version_fn()).to_string_lossy().into_owned();
        let blocks_version = version.split_whitespace().find_map(|w| w.strip_prefix("blocks=")?.parse().ok())
            .unwrap_or(crate::indexer::BLOCKS_VERSION);
        Ok(Dll {
            h: h as usize,
            version,
            blocks_version,
            parse: sym!(FnPathOptOut, b"sdp_parse\0"),
            extract_text: sym!(FnExtractText, b"sdp_extract_text\0"),
            ocr_pages: sym!(FnOcrPages, b"sdp_ocr_pages\0"),
            ocr_file: sym!(FnPathOptOut, b"sdp_ocr_file\0"),
            ocr_unload: sym!(FnHandle, b"sdp_ocr_unload\0"),
            ocr_cache_remove: sym!(FnCacheRemove, b"sdp_ocr_cache_remove\0"),
            status: sym!(FnStatus, b"sdp_get_status\0"),
            supports: sym!(FnSupports, b"sdp_supports\0"),
            set_option: sym!(FnSetOpt, b"sdp_set_option\0"),
            pdf_info: sym!(FnPathOut, b"sdp_pdf_info\0"),
            pdf_page_texts: sym!(FnPathOut, b"sdp_pdf_page_texts\0"),
            last_error,
            free: sym!(FnFree, b"sdp_free\0"),
            _lib: lib,
        })
    }
}

//------------------------------------------------------------------
// 적재한 DLL (처음 부를 때 한 번 적재)
//
// -out: Ok(&Dll)
// -out: error = 적재 실패 안내문(실패도 기억해 다시 시도하지 않는다)
//------------------------------------------------------------------
fn dll() -> Result<&'static Dll, String> {
    DLL.get_or_init(load).as_ref().map_err(|e| e.clone())
}

//------------------------------------------------------------------
// DLL 을 쓸 수 있나 (진단·시작 확인용)
//
// -out: Ok(버전 문자열) / Err(안내문)
//------------------------------------------------------------------
pub fn available() -> Result<String, String> {
    dll().map(|d| d.version.clone())
}

//------------------------------------------------------------------
// DLL 추출 결과 버전 — 색인 상태의 blocks_version (DLL 을 못 쓰면 SimpleRAG 기본값)
//------------------------------------------------------------------
pub fn blocks_version() -> i64 {
    dll().map(|d| d.blocks_version).unwrap_or(crate::indexer::BLOCKS_VERSION)
}

impl Dll {
    // 핸들 포인터
    fn hp(&self) -> *mut c_void {
        self.h as *mut c_void
    }

    //------------------------------------------------------------------
    // 결과 받기 — 상태 코드가 0 이면 JSON 을 읽고 문자열을 돌려준다, 아니면 마지막 오류문
    //
    // -in: rc = 상태 코드 / p = DLL 이 할당한 결과 글
    //
    // -out: Ok(Value)
    // -out: error = Err(DLL 오류문 — 앞머리 "OCRError:" 등은 그대로)
    //------------------------------------------------------------------
    fn take(&self, rc: i32, p: *mut c_char) -> Result<Value, String> {
        unsafe {
            if rc != 0 {
                let e = CStr::from_ptr((self.last_error)()).to_string_lossy().into_owned();
                return Err(if e.is_empty() { format!("문서 파서 오류({})", rc) } else { e });
            }
            if p.is_null() {
                return Ok(Value::Null);
            }
            let s = CStr::from_ptr(p).to_string_lossy().into_owned();
            (self.free)(p);
            serde_json::from_str(&s).map_err(|e| format!("문서 파서 결과 JSON 오류: {}", e))
        }
    }
}

// 문자열 → C 문자열(안의 NUL 은 지운다)
fn cs(s: &str) -> CString {
    CString::new(s.replace('\0', "")).unwrap_or_default()
}

//------------------------------------------------------------------
// 문서 파싱 → Document (docmodel::extract_document 의 본체)
//=> 결과의 ocr 상태는 ocr::note 로 남겨 색인(sync_doc)이 지금처럼 take_note 로 읽게 한다.
//
// -in: path  = 파일 경로
// -in: title = 문서 제목(None 이면 파일 이름에서)
// -in: keep  = PDF 에서 읽을 쪽(0부터). 있으면 그 쪽만, OCR 없이
//
// -out: Ok(Document)
// -out: error = DLL 적재 실패·파싱 실패면 Err(사유)
//------------------------------------------------------------------
pub fn parse(path: &str, title: Option<&str>, keep: Option<Vec<usize>>) -> Result<Document, String> {
    let d = dll()?;
    let mut opts = json!({});
    if let Some(t) = title {
        opts["title"] = json!(t);
    }
    if let Some(k) = &keep {
        opts["keep_pages"] = json!(k);
    }
    let (p, o) = (cs(path), cs(&opts.to_string()));
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe { (d.parse)(d.hp(), p.as_ptr(), o.as_ptr(), &mut out) };
    let v = match d.take(rc, out) {
        Ok(v) => v,
        Err(e) => {
            // 실패한 문서의 옛 OCR 상태는 지운다(지금과 같은 동작)
            if keep.is_none() {
                crate::ocr::note(path, crate::ocr::DocOcr::default());
            }
            return Err(e);
        }
    };
    let doc = doc_from_json(&v);
    if keep.is_none() {
        crate::ocr::note(path, doc.ocr.clone());
    }
    Ok(doc)
}

// 라벨 글 → 블록 종류 상수 (모르는 라벨은 문단으로)
fn kind_of(s: &str) -> &'static str {
    match s {
        "heading" => B::HEADING,
        "paragraph" => B::PARAGRAPH,
        "list_item" => B::LIST_ITEM,
        "table" => B::TABLE,
        "caption" => B::CAPTION,
        "code" => B::CODE,
        "header" => B::HEADER,
        "footer" => B::FOOTER,
        "page_number" => B::PAGE_NUMBER,
        "toc" => B::TOC,
        "note" => B::NOTE,
        _ => B::PARAGRAPH,
    }
}

// 출처 글 → 상수
fn source_of(s: Option<&str>) -> &'static str {
    match s {
        Some("inferred") => B::SRC_INFERRED,
        Some("regex") => B::SRC_REGEX,
        Some("ocr") => B::SRC_OCR,
        _ => B::SRC_PARSER,
    }
}

// JSON 숫자 배열 → usize 목록
fn usizes(v: Option<&Value>) -> Vec<usize> {
    v.and_then(|a| a.as_array()).map(|a| a.iter().filter_map(|x| x.as_u64()).map(|x| x as usize).collect()).unwrap_or_default()
}

//------------------------------------------------------------------
// 결과 JSON → Document (설계서 §6 모양)
//
// -in: v = sdp_parse 결과
//
// -out: Document (모르는 라벨은 문단 + 경고)
// -out: error = 예외 없음(빠진 필드는 기본값)
//------------------------------------------------------------------
pub fn doc_from_json(v: &Value) -> Document {
    let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
    let mut doc = Document::new(&s("format"), &s("title"));
    doc.pages = v.get("pages").and_then(|x| x.as_i64());
    doc.raw_text = v.get("raw_text").and_then(|x| x.as_str()).map(|x| x.to_string());
    doc.warnings = v.get("warnings").and_then(|x| x.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or_default();
    if let Some(o) = v.get("ocr") {
        doc.ocr = crate::ocr::DocOcr {
            pending: usizes(o.get("pending")),
            done: usizes(o.get("done")),
            whole: o.get("whole").and_then(|x| x.as_bool()).unwrap_or(false),
        };
    }
    let mut unknown = 0;
    for b in v.get("blocks").and_then(|x| x.as_array()).into_iter().flatten() {
        let t = b.get("type").and_then(|x| x.as_str()).unwrap_or("");
        if kind_of(t) == B::PARAGRAPH && t != "paragraph" {
            unknown += 1;
        }
        let mut blk = Block::new(kind_of(t), b.get("text").and_then(|x| x.as_str()).unwrap_or(""))
            .lv(b.get("level").and_then(|x| x.as_i64()).unwrap_or(0))
            .pg(b.get("page").and_then(|x| x.as_i64()))
            .src(source_of(b.get("source").and_then(|x| x.as_str())));
        if let Some(rows) = b.get("rows") {
            blk.rows = serde_json::from_value(rows.clone()).ok();
            blk.header_rows = b.get("header_rows").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
        }
        blk.synthetic = b.get("synthetic").and_then(|x| x.as_bool()).unwrap_or(false);
        doc.blocks.push(blk);
    }
    if unknown > 0 {
        doc.warnings.push(format!("모르는 블록 라벨 {}개 → 문단으로", unknown));
    }
    doc
}

//------------------------------------------------------------------
// 평문 추출 — (본문, 표 행 수) (extract::extract_text 의 본체)
//
// -in: path = 파일 / fix_tables = 표 줄 고치기
//
// -out: Ok((본문, 표 행 수))
// -out: error = 추출 실패면 Err(사유)
//------------------------------------------------------------------
pub fn extract_text(path: &str, fix_tables: bool) -> Result<(String, usize), String> {
    let d = dll()?;
    let p = cs(path);
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe { (d.extract_text)(d.hp(), p.as_ptr(), fix_tables as i32, &mut out) };
    let v = d.take(rc, out)?;
    Ok((v["text"].as_str().unwrap_or("").to_string(), v["table_rows"].as_u64().unwrap_or(0) as usize))
}

//------------------------------------------------------------------
// OCR 상태 — (켜짐, 그림 파일도 켜짐)
//=> 설정이 켜져 있고 모델 파일이 다 있을 때 켜짐. DLL 을 못 쓰면 꺼짐.
//
// -out: (enabled, images)
// -out: error = 예외 없음
//------------------------------------------------------------------
pub fn ocr_state() -> (bool, bool) {
    let Ok(d) = dll() else { return (false, false) };
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe { (d.status)(d.hp(), &mut out) };
    match d.take(rc, out) {
        Ok(v) => (v.pointer("/ocr/enabled").and_then(|x| x.as_bool()).unwrap_or(false),
                  v.pointer("/ocr/images").and_then(|x| x.as_bool()).unwrap_or(false)),
        Err(_) => (false, false),
    }
}

//------------------------------------------------------------------
// OCR 상태 JSON 전체 (진단용)
//------------------------------------------------------------------
pub fn status() -> Result<Value, String> {
    let d = dll()?;
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe { (d.status)(d.hp(), &mut out) };
    d.take(rc, out)
}

//------------------------------------------------------------------
// 블록 경로로 다루는 파일인가 (docmodel::supports)
//------------------------------------------------------------------
pub fn supports_blocks(path: &str) -> bool {
    let Ok(d) = dll() else { return false };
    let p = cs(path);
    unsafe { (d.supports)(d.hp(), p.as_ptr()) == 1 }
}

//------------------------------------------------------------------
// 실행 중 옵션 바꾸기 — 예: ("ocr.enabled", false)
//------------------------------------------------------------------
pub fn set_option(key: &str, value: &Value) -> Result<(), String> {
    let d = dll()?;
    let (k, v) = (cs(key), cs(&value.to_string()));
    let rc = unsafe { (d.set_option)(d.hp(), k.as_ptr(), v.as_ptr()) };
    d.take(rc, std::ptr::null_mut()).map(|_| ())
}

// 진행 콜백 받이 — user 는 &mut &mut dyn FnMut 를 가리킨다
unsafe extern "C" fn progress_tramp(k: i32, of: i32, ms: f64, user: *mut c_void) -> i32 {
    let f = &mut *(user as *mut &mut dyn FnMut(usize, usize, f64) -> bool);
    // 콜백 패닉이 DLL 로 넘어가지 않게 — 패닉이면 멈춤
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f(k.max(0) as usize, of.max(0) as usize, ms))) {
        Ok(stop) => stop as i32,
        Err(_) => 1,
    }
}

//------------------------------------------------------------------
// 쪽 OCR → 캐시 (색인의 OCR 대기열이 부른다)
//
// -in: path     = PDF · 그림 파일
// -in: pages    = 쪽 번호(1부터) — None 이면 남은 쪽 전부(그림 파일은 모든 쪽)
// -in: progress = 쪽마다 (k, of, ms) → true 면 그 쪽까지 하고 선다
//
// -out: Ok(멈췄나)
// -out: error = OCR 실패면 Err("OCRError: …") — 이미 끝낸 쪽은 캐시에 남는다
//------------------------------------------------------------------
pub fn ocr_pages(path: &str, pages: Option<&[usize]>, progress: &mut dyn FnMut(usize, usize, f64) -> bool) -> Result<bool, String> {
    let d = dll()?;
    let p = cs(path);
    let pj = pages.map(|x| cs(&json!(x).to_string()));
    let mut f: &mut dyn FnMut(usize, usize, f64) -> bool = progress;
    let user = &mut f as *mut &mut dyn FnMut(usize, usize, f64) -> bool as *mut c_void;
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe {
        (d.ocr_pages)(d.hp(), p.as_ptr(), pj.as_ref().map_or(std::ptr::null(), |s| s.as_ptr()), Some(progress_tramp), user, &mut out)
    };
    let v = d.take(rc, out)?;
    Ok(v["stopped"].as_bool().unwrap_or(false))
}

//------------------------------------------------------------------
// 파일 OCR 시험(캐시 안 씀) — `ocr <파일>` 명령
//
// -in: path = PDF · 그림 · 그림 폴더 / pages = 쪽 번호(1부터)
//
// -out: Ok({"file","engine","dpi","pages":{번호:{w,h,render_ms,ocr_ms,rows,lines}}})
// -out: error = Err(사유)
//------------------------------------------------------------------
pub fn ocr_file(path: &str, pages: Option<&[usize]>) -> Result<Value, String> {
    let d = dll()?;
    let p = cs(path);
    let pj = pages.map(|x| cs(&json!(x).to_string()));
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe { (d.ocr_file)(d.hp(), p.as_ptr(), pj.as_ref().map_or(std::ptr::null(), |s| s.as_ptr()), &mut out) };
    d.take(rc, out)
}

// OCR 엔진 내리기 — 메모리를 돌려준다
pub fn ocr_unload() {
    if let Ok(d) = dll() {
        unsafe { (d.ocr_unload)(d.hp()) };
    }
}

// OCR 캐시 지우기 — 파일 내용 SHA-1
pub fn ocr_cache_remove(hash: &str) {
    if let Ok(d) = dll() {
        let h = cs(hash);
        unsafe { (d.ocr_cache_remove)(d.hp(), h.as_ptr()) };
    }
}

//------------------------------------------------------------------
// PDF 속성 묶음 (meta::extract 의 pdf_meta 용)
// -필드: info = 문서 정보 사전(키, 값) / pages = 쪽 수 / toc = 책갈피 수 / text_chars = 글자층 글자 수
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct PdfMeta {
    pub info: Vec<(String, String)>,
    pub pages: i64,
    pub toc: i64,
    pub text_chars: i64,
}

//------------------------------------------------------------------
// PDF 속성 읽기
//
// -in: path = PDF
//
// -out: Ok(PdfMeta)
// -out: error = 암호·손상 PDF 면 Err("PdfiumError: …")
//------------------------------------------------------------------
pub fn pdf_meta(path: &str) -> Result<PdfMeta, String> {
    let d = dll()?;
    let p = cs(path);
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe { (d.pdf_info)(d.hp(), p.as_ptr(), &mut out) };
    let v = d.take(rc, out)?;
    let info = v["info"].as_array().into_iter().flatten().filter_map(|kv| {
        Some((kv.get(0)?.as_str()?.to_string(), kv.get(1)?.as_str()?.to_string()))
    }).collect();
    Ok(PdfMeta { info, pages: v["pages"].as_i64().unwrap_or(0), toc: v["toc"].as_i64().unwrap_or(0),
                 text_chars: v["text_chars"].as_i64().unwrap_or(0) })
}

//------------------------------------------------------------------
// PDF 쪽별 글자층 글 (find.rs 의 쪽 찾기 용)
//
// -in: path = PDF
//
// -out: Ok(쪽 글 목록)
// -out: error = 열기 실패면 Err(사유)
//------------------------------------------------------------------
pub fn pdf_page_texts(path: &str) -> Result<Vec<String>, String> {
    let d = dll()?;
    let p = cs(path);
    let mut out: *mut c_char = std::ptr::null_mut();
    let rc = unsafe { (d.pdf_page_texts)(d.hp(), p.as_ptr(), &mut out) };
    let v = d.take(rc, out)?;
    Ok(v["pages"].as_array().into_iter().flatten().map(|x| x.as_str().unwrap_or("").to_string()).collect())
}
