//------------------------------------------------------------------
// 기존 추출 경로 마무리 (Python simplerag/extract.py)
//=> 기존 추출기(legacy.rs) 결과에 SimpleRAG 만의 손질을 더한다.
//    1) 정제(sanitize) — 토크나이저를 죽이는 비문자·사용자 정의 영역·외톨이 서로게이트 제거
//    2) 표 복원(normalize_tables) — DOC·XLS 의 탭 구분 셀을 "| a | b |" 줄로 바꾼다
//    3) .ipynb 는 소스·마크다운 셀만 뽑는다
//------------------------------------------------------------------

use crate::pyutil;

const MIN_STACK: usize = 3; // 이만큼 줄이 쌓여야 '눕힌 행'으로 본다
const MAX_STACK: usize = 60;

//------------------------------------------------------------------
// 탭 구분 표 → 마크다운 표 줄
//=> "\t\t" 를 행 경계로 보고, 행 안의 탭을 칸 경계로 본다. 결과가 원문보다 글자를 잃으면
//   (공백 제외 글자 수 비교) 원문을 그대로 쓴다 — ARIA-WISC.pdf 93% 손실 사고 방어.
//
// -in: text = 추출 원문
//
// -out: (본문, 만든 표 행 수)
// -out: error = 없음
//------------------------------------------------------------------
pub fn normalize_tables(text: &str) -> (String, usize) {
    if !text.contains('\t') {
        return (text.to_string(), 0);
    }
    let mut out: Vec<String> = vec![];
    let mut made = 0;
    for part in text.split("\t\t") {
        if !part.contains('\t') {
            out.push(part.to_string());
            continue;
        }
        let mut cells: Vec<String> = part.split('\t').map(|s| s.to_string()).collect();
        if cells[0].contains('\n') {
            let (head, _, first) = pyutil::rpartition(&cells[0], "\n");
            let (head, first) = (head.to_string(), first.to_string());
            if !pyutil::strip(&head).is_empty() {
                out.push(head);
            }
            cells[0] = first;
        }
        let rows = expand_row(&cells);
        if rows.is_empty() {
            out.push(part.to_string());
            continue;
        }
        for r in rows {
            out.push(format!("| {} |", r.join(" | ")));
            made += 1;
        }
    }
    let result = out.join("\n");
    if content_len(&result) < content_len(text) {
        return (text.to_string(), 0);
    }
    (result, made)
}

// 공백 아닌 글자 수
fn content_len(s: &str) -> usize {
    s.chars().filter(|c| !pyutil::is_space(*c)).count()
}

//------------------------------------------------------------------
// 행 하나 펼치기 (_expand_row)
//=> 줄이 3줄 이상 쌓인 칸이 2개 이상이면 줄끼리 짝지어 여러 행으로 펼친다(눕힌 행).
//   한 줄짜리 칸은 모든 행에 되풀이한다. 아니면 칸 안 줄바꿈을 공백으로 바꿔 한 행으로.
//
// -in: cells = 칸 목록
//
// -out: 행 목록 (내용 있는 칸이 2개 미만이면 [])
// -out: error = 없음
//------------------------------------------------------------------
pub fn expand_row(cells: &[String]) -> Vec<Vec<String>> {
    let cells: Vec<String> = cells.iter().map(|c| c.trim_matches('\r').to_string()).collect();
    if cells.iter().filter(|c| !pyutil::strip(c).is_empty()).count() < 2 {
        return vec![];
    }
    let counts: Vec<usize> = cells.iter().map(|c| c.split('\n').count()).collect();
    let stacked: Vec<usize> = counts.iter().copied().filter(|&n| n >= MIN_STACK).collect();
    if stacked.len() >= 2 && *stacked.iter().max().unwrap() <= MAX_STACK {
        let n = *stacked.iter().max().unwrap();
        let mut rows = vec![];
        for i in 0..n {
            let row: Vec<String> = cells.iter().map(|c| {
                let lines: Vec<&str> = c.split('\n').collect();
                if lines.len() == 1 {
                    flat(lines[0])
                } else if i < lines.len() {
                    flat(lines[i])
                } else {
                    String::new()
                }
            }).collect();
            if row.iter().any(|x| !x.is_empty()) {
                rows.push(row);
            }
        }
        return rows;
    }
    vec![cells.iter().map(|c| flat(&c.replace('\n', " "))).collect()]
}

// 공백 한 칸으로 접기
fn flat(s: &str) -> String {
    pyutil::collapse_ws(s)
}

//------------------------------------------------------------------
// .ipynb 셀 글자
//------------------------------------------------------------------
pub fn extract_ipynb(path: &str) -> Result<String, String> {
    let raw = std::fs::read(path).map_err(|e| e.to_string())?;
    let s = String::from_utf8_lossy(&raw);
    let nb: serde_json::Value = serde_json::from_str(s.trim_start_matches('\u{feff}')).map_err(|e| format!("JSONDecodeError: {}", e))?;
    let mut parts = vec![];
    if let Some(cells) = nb.get("cells").and_then(|c| c.as_array()) {
        for cell in cells {
            let body = cell_source(cell);
            if !pyutil::strip(&body).is_empty() {
                parts.push(body);
            }
        }
    }
    Ok(parts.join("\n\n"))
}

// 셀 source(목록이면 이어 붙임)
pub fn cell_source(cell: &serde_json::Value) -> String {
    match cell.get("source") {
        Some(serde_json::Value::Array(a)) => a.iter().map(|x| x.as_str().map(|s| s.to_string()).unwrap_or_else(|| x.to_string())).collect(),
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
        None => String::new(),
    }
}

//------------------------------------------------------------------
// 정제 — 비문자(U+FDD0~FDEF, U+FFFE/FFFF)·사용자 정의 영역(U+E000~F8FF)을 공백으로
//=> Rust 문자열에는 외톨이 서로게이트가 들어올 수 없으므로 그 단계는 필요 없다.
//
// -in: text = 원문
//
// -out: (정제한 글자, 바꾼 글자 수)
// -out: error = 없음
//------------------------------------------------------------------
pub fn sanitize(text: &str) -> (String, usize) {
    let bad = |c: char| matches!(c as u32, 0xFDD0..=0xFDEF | 0xFFFE | 0xFFFF | 0xE000..=0xF8FF);
    if !text.chars().any(bad) {
        return (text.to_string(), 0);
    }
    let mut n = 0;
    let s = text.chars().map(|c| if bad(c) { n += 1; ' ' } else { c }).collect();
    (s, n)
}

//------------------------------------------------------------------
// 기존 추출 진입점 — (본문, 표 행 수)
//
// -in: path       = 파일 경로
// -in: fix_tables = 표 복원을 할지
//
// -out: Ok((본문, 표 행 수))
// -out: error = 추출 실패면 Err(사유)
//------------------------------------------------------------------
pub fn extract_text(path: &str, fix_tables: bool) -> Result<(String, usize), String> {
    if pyutil::ext_lower(path) == ".ipynb" {
        let (t, _) = sanitize(&extract_ipynb(path)?);
        return Ok((t, 0));
    }
    let (text, _) = sanitize(&crate::legacy::extract(path)?);
    if text.is_empty() || !fix_tables {
        return Ok((text, 0));
    }
    Ok(normalize_tables(&text))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tables() {
        let (t, n) = normalize_tables("머리\n이름\t값\t\t홍길동\t30");
        assert_eq!(n, 2);
        assert_eq!(t, "머리\n| 이름 | 값 |\n| 홍길동 | 30 |");
    }
}
