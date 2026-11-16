//------------------------------------------------------------------
// 구조화 추출 입구 (추출 본체는 struct_docparse.dll 로 옮김 — 설계서 2026-11-16)
//=> 파일 하나를 Document(블록 목록)로 만든다. 형식 판정 · 파서 · 후처리 · 폴백 평문 · 스캔 OCR 은 모두 DLL 이 하고,
//   여기는 같은 함수 모양을 지켜 호출처(indexer · cli · chunk)가 그대로 쓰게 한다.
//   절 조립(assemble)과 블록 모델(blocks)은 검색용 가공이라 SimpleRAG 에 남는다.
//------------------------------------------------------------------

pub mod assemble;
pub mod blocks;

use blocks::Document;

use crate::pyutil;

//------------------------------------------------------------------
// 블록 경로로 처리하는 확장자인가
//=> 그림 파일은 OCR 이 켜져 있을 때만 블록 경로다(DLL 판정과 같음).
//
// -in: path = 파일 경로
//
// -out: true = 블록 경로
// -out: error = 예외 없음(DLL 을 못 쓰면 false)
//------------------------------------------------------------------
pub fn supports(path: &str) -> bool {
    if crate::ocr::image::is_image(path) {
        return crate::ocr::images_enabled();
    }
    crate::docparse::supports_blocks(path)
}

//------------------------------------------------------------------
// 문서 하나 추출 (핵심)
//
// -in: path  = 파일 경로
// -in: title = 문서 제목(None 이면 파일명에서 확장자를 뗀 것)
//
// -out: Ok(Document) — 블록 경로 또는 폴백 평문(format "legacy")
// -out: error = 폴백 추출까지 실패하거나 DLL 을 못 쓰면 Err(사유)
//------------------------------------------------------------------
pub fn extract_document(path: &str, title: Option<&str>) -> Result<Document, String> {
    let title = title.map(|t| t.to_string()).unwrap_or_else(|| pyutil::splitext(pyutil::basename(path)).0.to_string());
    crate::docparse::parse(path, Some(&title), None)
}

//------------------------------------------------------------------
// 문서 추출 — PDF 는 고른 쪽만 (비슷한 문서 찾기의 큰 바깥 PDF)
//=> PDF 가 아니거나 keep 이 None 이면 extract_document 와 같다. 고른 쪽만 읽을 때는 OCR 하지 않는다.
//   DLL 에는 쪽 번호 목록으로 넘겨야 하므로, 먼저 쪽 수를 알아 keep 을 쪽마다 물어 목록을 만든다.
//
// -in: path · title = extract_document 와 같음 / keep = 쪽 번호(0부터), 전체 쪽 수 → 읽을지
//
// -out: Ok(Document)
// -out: error = 추출 실패 문자열
//------------------------------------------------------------------
pub fn extract_document_keep(path: &str, title: Option<&str>, keep: Option<&dyn Fn(usize, usize) -> bool>) -> Result<Document, String> {
    let keep = match keep {
        Some(k) if pyutil::ext_lower(path) == ".pdf" => k,
        _ => return extract_document(path, title),
    };
    let title = title.map(|t| t.to_string()).unwrap_or_else(|| pyutil::splitext(pyutil::basename(path)).0.to_string());
    // 쪽 수 — PDF 속성에서. 읽지 못하면 DLL 파싱이 같은 오류를 낸다
    let n = crate::docparse::pdf_meta(path).map(|m| m.pages.max(0) as usize).unwrap_or(0);
    let pages: Vec<usize> = (0..n).filter(|&i| keep(i, n)).collect();
    crate::docparse::parse(path, Some(&title), Some(pages))
}

//------------------------------------------------------------------
// 진단용 블록 JSON 저장 — <폴더>/<이름>.blocks.json
//------------------------------------------------------------------
pub fn dump_json(doc: &Document, out_dir: &str, name: &str) -> std::io::Result<String> {
    std::fs::create_dir_all(out_dir)?;
    let path = pyutil::join(out_dir, &format!("{}.blocks.json", name));
    let s = serde_json::to_string_pretty(&doc.to_json()).unwrap_or_default();
    std::fs::write(&path, s)?;
    Ok(path)
}
