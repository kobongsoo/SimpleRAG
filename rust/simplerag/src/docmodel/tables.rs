//------------------------------------------------------------------
// 표 행렬 도우미 (Python docmodel/tables.py)
//=> 포맷마다 표 저장 방식은 달라도 결과는 "행 × 칸 글자 행렬"로 맞춘다.
//   병합 셀 펼치기, 빈 행·열 걷기, 머리글 행 추정, "| a | b |" 렌더링을 한곳에 둔다.
//------------------------------------------------------------------

use once_cell::sync::Lazy;
use regex::Regex;

use crate::pyutil;

static NUMERIC: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[-\d,.\s%/:()~]*\d[-\d,.\s%/:()~]*$").unwrap());

//------------------------------------------------------------------
// 셀 글자 한 줄로 다듬기 — 공백을 한 칸으로, '|' 는 전각 '｜' 로
//------------------------------------------------------------------
pub fn clean_cell(s: &str) -> String {
    pyutil::collapse_ws(s).replace('|', "｜")
}

// 빈 격자(None 은 아직 안 채운 칸)
pub fn new_grid(n_rows: usize, n_cols: usize) -> Vec<Vec<Option<String>>> {
    vec![vec![None; n_cols]; n_rows]
}

//------------------------------------------------------------------
// 격자에 셀 놓기(병합 펼치기)
//=> 세로 병합은 아래 행마다 같은 글자를 되풀이하고, 가로 병합은 첫 칸에만 글자를 둔다.
//------------------------------------------------------------------
pub fn place(grid: &mut [Vec<Option<String>>], r: i64, c: i64, text: &str, rowspan: i64, colspan: i64) {
    let rowspan = rowspan.max(1);
    let colspan = colspan.max(1);
    for dr in 0..rowspan {
        let rr = r + dr;
        if rr < 0 || rr as usize >= grid.len() {
            continue;
        }
        let row = &mut grid[rr as usize];
        for dc in 0..colspan {
            let cc = c + dc;
            if cc < 0 || cc as usize >= row.len() {
                continue;
            }
            row[cc as usize] = Some(if dc == 0 { text.to_string() } else { String::new() });
        }
    }
}

//------------------------------------------------------------------
// 격자 → 행 목록 (빈 행·빈 열 걷기, 셀 안 줄바꿈은 둔다)
//------------------------------------------------------------------
pub fn finish(grid: &[Vec<Option<String>>]) -> Vec<Vec<String>> {
    let rows: Vec<Vec<String>> = grid
        .iter()
        .map(|row| row.iter().map(|x| pyutil::strip(x.as_deref().unwrap_or("")).to_string()).collect::<Vec<_>>())
        .filter(|row: &Vec<String>| row.iter().any(|c| !c.is_empty()))
        .collect();
    if rows.is_empty() {
        return vec![];
    }
    let width = rows.iter().map(|r| r.len()).max().unwrap_or(0);
    let rows: Vec<Vec<String>> = rows.into_iter().map(|mut r| {
        r.resize(width, String::new());
        r
    }).collect();
    let keep: Vec<usize> = (0..width).filter(|&j| rows.iter().any(|r| !r[j].is_empty())).collect();
    rows.into_iter().map(|r| keep.iter().map(|&j| r[j].clone()).collect()).collect()
}

// 숫자(와 단위 기호)만으로 된 칸인가
fn is_number(cell: &str) -> bool {
    !cell.is_empty() && NUMERIC.is_match(cell)
}

//------------------------------------------------------------------
// 머리글 행처럼 보이는가 — 이름표 칸 2개 이상, 숫자 칸 없음
//------------------------------------------------------------------
pub fn looks_like_header(row: &[String]) -> bool {
    let named: Vec<&String> = row.iter().filter(|c| !c.is_empty()).collect();
    if named.len() < 2 {
        return false;
    }
    !named.iter().any(|c| is_number(c))
}

//------------------------------------------------------------------
// 머리글 행 수 — 파일이 표시했으면 그 값, 없으면 첫 행이 머리글처럼 생겼을 때만 1
//------------------------------------------------------------------
pub fn header_rows(rows: &[Vec<String>], declared: usize) -> usize {
    if declared > 0 {
        return if declared < rows.len() { declared } else { 0 };
    }
    if rows.len() > 1 && looks_like_header(&rows[0]) {
        1
    } else {
        0
    }
}

pub fn render_row(row: &[String]) -> String {
    format!("| {} |", row.join(" | "))
}

pub fn render(rows: &[Vec<String>]) -> String {
    rows.iter().map(|r| render_row(r)).collect::<Vec<_>>().join("\n")
}
