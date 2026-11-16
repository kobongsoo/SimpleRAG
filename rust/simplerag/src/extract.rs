//------------------------------------------------------------------
// 기존(평문) 추출 입구 (본체는 struct_docparse.dll 로 옮김 — 설계서 2026-11-16)
//=> 정제(sanitize) · 표 복원(normalize_tables) · .ipynb 셀 뽑기는 DLL 의 sdp_extract_text 가 그대로 한다.
//------------------------------------------------------------------

//------------------------------------------------------------------
// 기존 추출 진입점 — (본문, 표 행 수)
//
// -in: path       = 파일 경로
// -in: fix_tables = 표 복원을 할지
//
// -out: Ok((본문, 표 행 수))
// -out: error = 추출 실패·DLL 을 못 쓰면 Err(사유)
//------------------------------------------------------------------
pub fn extract_text(path: &str, fix_tables: bool) -> Result<(String, usize), String> {
    crate::docparse::extract_text(path, fix_tables)
}
