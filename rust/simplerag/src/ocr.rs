//------------------------------------------------------------------
// 스캔 문서·그림 파일 OCR — SimpleRAG 쪽 남은 부분 (OCR 본체는 struct_docparse.dll 로 옮김, 설계서 2026-11-16)
//=> 엔진·캐시·쪽 판정은 DLL 이 한다. 여기에는 색인이 쓰는 것만 남는다.
//    · 켜짐 판정(enabled · images_enabled) — DLL 상태를 묻는다
//    · 추출 → 색인 사이의 OCR 상태 표(note · take_note) — 준비 스레드가 추출하고 sync_doc 이 읽는다
//    · 그림 파일 확장자 · 엔진 내리기 · 캐시 지우기
//------------------------------------------------------------------

use std::sync::Mutex;

use crate::pyutil;

//------------------------------------------------------------------
// 그림 파일 판정 (DLL 의 ocr::image 와 같은 목록)
//------------------------------------------------------------------
pub mod image {
    // OCR 대상 그림 확장자
    pub const IMAGE_EXTS: &[&str] = &[".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"];

    // 이 경로가 OCR 대상 그림 파일인가(확장자만 본다)
    pub fn is_image(path: &str) -> bool {
        let e = crate::pyutil::ext_lower(path);
        IMAGE_EXTS.contains(&e.as_str())
    }
}

//------------------------------------------------------------------
// OCR 을 쓸 수 있나 — 설정이 켜져 있고 모델 파일이 다 있을 때 (DLL 이 판정)
//=> 설정이 꺼져 있으면 DLL 을 부르지 않는다. 모델이 없으면 DLL 이 로그에 한 번 경고한다.
//------------------------------------------------------------------
pub fn enabled() -> bool {
    crate::config::cfg().ocr && crate::docparse::ocr_state().0
}

// 그림 파일도 OCR 하나 — ocr 이 켜져 있고 ocr_images 도 켜져 있을 때
pub fn images_enabled() -> bool {
    crate::config::cfg().ocr_images && crate::config::cfg().ocr && crate::docparse::ocr_state().1
}

// 엔진 내리기 — 대기열을 다 처리했거나 멈출 때 메모리를 돌려준다
pub fn unload() {
    crate::docparse::ocr_unload();
}

// 캐시 지우기 — 파일 내용 SHA-1
pub fn remove_cache(hash: &str) {
    crate::docparse::ocr_cache_remove(hash);
}

// ── 추출 → 색인 사이의 OCR 정보 ─────────────────────────────

//------------------------------------------------------------------
// 문서 하나의 OCR 상태 (DLL 결과의 ocr 필드 — 추출이 남기고 sync_doc 이 읽는다)
// -필드: pending = 아직 OCR 하지 않은 쪽(1부터, 그림 파일은 [1]) — 비어 있지 않으면 대기열로 간다
// -필드: done    = OCR 글이 들어간 쪽(1부터) — 그 쪽 청크에 ocr 표시를 단다
// -필드: whole   = 그림 파일처럼 모든 청크가 OCR 글인가
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct DocOcr {
    pub pending: Vec<usize>,
    pub done: Vec<usize>,
    pub whole: bool,
}

// 추출은 준비 스레드에서도 돌므로 스레드 사이에서 보이는 표에 남긴다(열쇠 = 절대 경로)
static DOC_OCR: Mutex<Option<std::collections::HashMap<String, DocOcr>>> = Mutex::new(None);

//------------------------------------------------------------------
// 추출 결과의 OCR 상태 남기기 — 비어 있으면 지운다
//
// -in: path = 문서 경로 / info = OCR 상태
//
// -out: 없음
// -out: error = 예외 없음
//------------------------------------------------------------------
pub fn note(path: &str, info: DocOcr) {
    let mut g = DOC_OCR.lock().unwrap_or_else(|p| p.into_inner());
    let m = g.get_or_insert_with(Default::default);
    let k = pyutil::abspath(path);
    if info.pending.is_empty() && info.done.is_empty() && !info.whole {
        m.remove(&k);
    } else {
        m.insert(k, info);
    }
}

//------------------------------------------------------------------
// 남긴 OCR 상태 꺼내기(꺼내면 지운다)
//
// -in: path = 문서 경로
//
// -out: DocOcr (없으면 빈 값)
// -out: error = 예외 없음
//------------------------------------------------------------------
pub fn take_note(path: &str) -> DocOcr {
    let mut g = DOC_OCR.lock().unwrap_or_else(|p| p.into_inner());
    g.get_or_insert_with(Default::default).remove(&pyutil::abspath(path)).unwrap_or_default()
}
