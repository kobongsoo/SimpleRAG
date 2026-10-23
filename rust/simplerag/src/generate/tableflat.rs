//------------------------------------------------------------------
// 표 행을 "머리글: 값" 평문으로 펴기 (Python tableflat.py, 계획서 3-3)
//=> 근거를 모델에 보여 줄 때만 쓴다(색인은 그대로). 병합 셀 탓에 값이 한 칸씩 밀리는 표에서
//   모델이 옆 칸 값을 집는 실패를 줄인다. 글자가 줄면 원문을 그대로 쓴다.
//------------------------------------------------------------------

use once_cell::sync::Lazy;
use regex::Regex;

use crate::pyutil;

static TABLE_LINE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[ \t]*\|.*\|[ \t]*$").unwrap());
static NUMERIC: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[-\d,.\s]*[\d][-\d,.\s]*$").unwrap());
static CONTENT: Lazy<Regex> = Lazy::new(|| Regex::new(r"[\s|]+").unwrap());
const MAX_CELL_CHARS: usize = 40;
const MAX_COLS: usize = 12;

fn cells(line: &str) -> Vec<String> {
    let mut inner = pyutil::strip(line);
    if let Some(s) = inner.strip_prefix('|') {
        inner = s;
    }
    if let Some(s) = inner.strip_suffix('|') {
        inner = s;
    }
    inner.split('|').map(|c| pyutil::strip(c).to_string()).collect()
}
fn is_number(c: &str) -> bool {
    !c.is_empty() && NUMERIC.is_match(c)
}
fn is_header(cells: &[String]) -> bool {
    let named: Vec<&String> = cells.iter().filter(|c| !c.is_empty()).collect();
    named.len() >= 2 && !named.iter().any(|c| is_number(c))
}
fn align(cells: &[String], header: &[String]) -> Option<Vec<String>> {
    let mut c = cells.to_vec();
    while c.last().map(|x| x.is_empty()).unwrap_or(false) {
        c.pop();
    }
    if c.is_empty() {
        return None;
    }
    if c.len() > 1 && c[0].is_empty() && !header.is_empty() && !is_number(&header[0]) && !is_number(&c[1]) {
        c.remove(0);
    }
    if c.len() > header.len() {
        return None;
    }
    c.resize(header.len(), String::new());
    Some(c)
}
fn render(cells: &[String], header: &[String]) -> String {
    header.iter().zip(cells).filter(|(_, v)| !v.is_empty())
        .map(|(n, v)| if n.is_empty() { v.clone() } else { format!("{}: {}", n, v) })
        .collect::<Vec<_>>().join(" / ")
}
fn content(s: &str) -> usize {
    pyutil::clen(&CONTENT.replace_all(s, ""))
}

// 표 줄 덩어리 하나 펴기(펼 것이 없으면 None)
fn flatten_run(run: &[&str]) -> Option<Vec<String>> {
    let rows: Vec<Vec<String>> = run.iter().map(|l| cells(l)).collect();
    let head_at = rows.iter().position(|r| is_header(r))?;
    let header = &rows[head_at];
    if header.len() > MAX_COLS {
        return None;
    }
    let mut out: Vec<(char, String)> = vec![];
    let mut all_flat = true;
    for (k, c) in rows.iter().enumerate() {
        if k == head_at {
            out.push(('H', run[k].to_string()));
            continue;
        }
        if c.iter().any(|x| pyutil::clen(x) > MAX_CELL_CHARS) {
            out.push(('R', run[k].to_string()));
            all_flat = false;
            continue;
        }
        match align(c, header) {
            None => {
                out.push(('R', run[k].to_string()));
                all_flat = false;
            }
            Some(a) => {
                let line = render(&a, header);
                if line.is_empty() {
                    out.push(('R', run[k].to_string()));
                    all_flat = false;
                } else {
                    out.push(('F', line));
                }
            }
        }
    }
    if !out.iter().any(|(t, _)| *t == 'F') {
        return None;
    }
    Some(out.into_iter().filter(|(t, _)| !(all_flat && *t == 'H')).map(|(_, s)| s).collect())
}

//------------------------------------------------------------------
// 진입점 — 글 안의 모든 표 덩어리를 편다
//------------------------------------------------------------------
pub fn flatten_tables(text: &str) -> String {
    if text.is_empty() || !text.contains('|') {
        return text.to_string();
    }
    let lines: Vec<&str> = text.split('\n').collect();
    let mut out: Vec<String> = vec![];
    let mut changed = false;
    let mut i = 0;
    while i < lines.len() {
        if !TABLE_LINE.is_match(lines[i]) {
            out.push(lines[i].to_string());
            i += 1;
            continue;
        }
        let mut j = i;
        while j < lines.len() && TABLE_LINE.is_match(lines[j]) {
            j += 1;
        }
        match flatten_run(&lines[i..j]) {
            None => out.extend(lines[i..j].iter().map(|s| s.to_string())),
            Some(f) => {
                out.extend(f);
                changed = true;
            }
        }
        i = j;
    }
    if !changed {
        return text.to_string();
    }
    let result = out.join("\n");
    if content(&result) < content(text) {
        return text.to_string();
    }
    result
}
