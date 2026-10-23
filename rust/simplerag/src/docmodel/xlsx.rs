//------------------------------------------------------------------
// XLSX → 블록 (Python docmodel/ooxml_xlsx.py)
//=> 시트 하나가 절 하나(합성 제목 = 시트 이름). 시트 안에서 빈 열로 갈라진 표는 따로 나누고,
//   표 위의 한 칸짜리 줄은 문단으로 뺀다. 세로 병합은 아래 행에 되풀이, 셀 메모는 끝에 따로.
//------------------------------------------------------------------

use std::collections::{BTreeMap, HashMap, HashSet};

use once_cell::sync::Lazy;
use regex::Regex;

use super::blocks::{self as B, Block, Document};
use super::ooxml::{self, cell_ref};
use super::tables;
use crate::pyutil;
use crate::xmldom::Node;

static COMMENTS: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)xl/comments(\d*)\.xml$").unwrap());
static TCOMMENTS: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)xl/threadedComments/threadedComment(\d*)\.xml$").unwrap());
static SHEET: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)xl/worksheets/sheet(\d+)\.xml$").unwrap());
const MAX_ROWS: i64 = 1048576;
const MAX_COLS: i64 = 256;
const MAX_MERGE_ROWS: i64 = 500;
const MIN_TABLE_COLS: usize = 2;

//------------------------------------------------------------------
// XLSX 추출 (진입점)
//------------------------------------------------------------------
pub fn extract(path: &str, title: &str) -> Result<Document, String> {
    let mut z = ooxml::open(path, "xlsx")?;
    let names: HashSet<String> = ooxml::names(&z).into_iter().collect();
    let shared = shared_strings(&mut z, &names);
    let name_vec: Vec<String> = names.iter().cloned().collect();
    let styles = crate::legacy::xlsx_date_styles(&mut z, &name_vec);
    let sheets = sheet_order(&mut z, &names);
    if sheets.is_empty() {
        return Err("xlsx 시트 없음".into());
    }
    let mut doc = Document::new("xlsx", title);
    doc.pages = Some(sheets.len() as i64);
    for (i, (name, part)) in sheets.iter().enumerate() {
        let no = i as i64 + 1;
        let root = match ooxml::parse(&mut z, part) {
            Some(r) => r,
            None => {
                doc.warnings.push(format!("시트 {} XML 손상", name));
                continue;
            }
        };
        let label = if name.is_empty() { format!("시트 {}", no) } else { name.clone() };
        let mut h = Block::new(B::HEADING, label).lv(1).pg(Some(no));
        h.synthetic = true;
        doc.blocks.push(h);
        for mut rows in sheet_tables(&root, &shared, &styles, &mut doc.warnings, name) {
            while !rows.is_empty() && rows[0].iter().filter(|c| !pyutil::strip(c).is_empty()).count() <= 1 {
                let first = rows.remove(0);
                let line = first.into_iter().find(|c| !pyutil::strip(c).is_empty()).unwrap_or_default();
                if !line.is_empty() {
                    doc.blocks.push(Block::new(B::PARAGRAPH, line).pg(Some(no)));
                }
            }
            if !rows.is_empty() {
                doc.blocks.push(Block::table(rows, 0).pg(Some(no)));
            }
        }
    }
    let memos = comments(&mut z, &names);
    if !memos.is_empty() {
        let mut h = Block::new(B::HEADING, "셀 메모").lv(1);
        h.synthetic = true;
        doc.blocks.push(h);
        doc.blocks.extend(memos.into_iter().map(|m| Block::new(B::NOTE, m)));
    }
    Ok(doc)
}

// 시트 순서 — workbook.xml 의 sheet 순서(이름과 함께)
fn sheet_order(z: &mut ooxml::Zip, names: &HashSet<String>) -> Vec<(String, String)> {
    if let Some(wb) = ooxml::parse(z, "xl/workbook.xml") {
        let rmap = ooxml::rels(z, "xl/workbook.xml");
        let mut out = vec![];
        for e in wb.iter_tag("sheet") {
            let rid = e.attrs.iter().find(|(k, v)| k.rsplit(':').next() == Some("id") && rmap.iter().any(|(id, _)| id == v));
            if let Some((_, rid)) = rid {
                if let Some((_, (_, full))) = rmap.iter().find(|(id, _)| id == rid) {
                    if names.contains(full) {
                        out.push((e.get("name").unwrap_or("").to_string(), full.clone()));
                    }
                }
            }
        }
        if !out.is_empty() {
            return out;
        }
    }
    let mut v: Vec<String> = names.iter().filter(|n| SHEET.is_match(n)).cloned().collect();
    v.sort_by_key(|n| ooxml::num_key(n, &SHEET));
    v.into_iter().map(|n| (String::new(), n)).collect()
}

// 공유 문자열 — 후리가나(rPh) 안 글자는 뺀다
fn shared_strings(z: &mut ooxml::Zip, names: &HashSet<String>) -> Vec<String> {
    if !names.contains("xl/sharedStrings.xml") {
        return vec![];
    }
    let root = match ooxml::parse(z, "xl/sharedStrings.xml") {
        Some(r) => r,
        None => return vec![],
    };
    let mut out = vec![];
    for si in root.children.iter().filter(|s| s.tag == "si") {
        let mut s = String::new();
        fn walk(n: &Node, s: &mut String, in_rph: bool) {
            let rph = in_rph || n.tag == "rPh";
            if n.tag == "t" && !rph {
                s.push_str(&n.text);
            }
            for c in &n.children {
                walk(c, s, rph);
            }
        }
        // si 자신도 iter() 에 들어간다 — si 의 직계 rPh 아래만 후리가나
        if si.tag == "t" {
            s.push_str(&si.text);
        }
        for c in &si.children {
            walk(c, &mut s, false);
        }
        out.push(s);
    }
    out
}

// 셀 값
fn cell_value(c: &Node, shared: &[String], styles: &[u8]) -> String {
    let t = c.get("t");
    let v = c.child("v");
    if t == Some("s") {
        return v.and_then(|v| pyutil::strip(&v.text).parse::<usize>().ok()).and_then(|i| shared.get(i).cloned()).unwrap_or_default();
    }
    if t == Some("inlineStr") {
        return c.iter().into_iter().filter(|e| e.tag == "t").map(|e| e.text.as_str()).collect();
    }
    let v = match v {
        Some(v) => v,
        None => return String::new(),
    };
    let raw = v.text.clone();
    if (t.is_none() || t == Some("n")) && !styles.is_empty() {
        let kind = c.get("s").and_then(|s| pyutil::strip(s).parse::<usize>().ok()).and_then(|i| styles.get(i).copied())
            .unwrap_or(crate::xlsdate::KIND_NONE);
        if let Some(shown) = crate::xlsdate::serial_to_string(&raw, kind) {
            return shown;
        }
    }
    raw
}

// 시트 → 행 격자(열 자리 유지)
fn sheet_rows(root: &Node, shared: &[String], styles: &[u8], warnings: &mut Vec<String>, name: &str) -> Vec<Vec<String>> {
    let mut cells: HashMap<(i64, i64), String> = HashMap::new();
    let mut cut = false;
    for row in root.iter() {
        if row.tag != "row" {
            continue;
        }
        let mut auto_c = 0i64;
        let auto_r = row.get("r").and_then(|r| pyutil::strip(r).parse::<i64>().ok()).map(|r| r - 1);
        for c in row.children.iter().filter(|c| c.tag == "c") {
            let pos = match cell_ref(c.get("r").unwrap_or("")) {
                Some(p) => p,
                None => match auto_r {
                    Some(r) => (r, auto_c),
                    None => continue,
                },
            };
            let (r, col) = pos;
            auto_c = col + 1;
            if r >= MAX_ROWS || col >= MAX_COLS {
                cut = true;
                continue;
            }
            let val = cell_value(c, shared, styles);
            if !pyutil::strip(&val).is_empty() {
                cells.insert((r, col), val);
            }
        }
    }
    if cut {
        warnings.push(format!("시트 {} — {}행·{}열을 넘는 부분 생략", name, MAX_ROWS, MAX_COLS));
    }
    for mc in root.iter_tag("mergeCell") {
        let refs: Vec<&str> = mc.get("ref").unwrap_or("").split(':').collect();
        if refs.len() != 2 {
            continue;
        }
        let (a, b) = match (cell_ref(refs[0]), cell_ref(refs[1])) {
            (Some(a), Some(b)) => (a, b),
            _ => continue,
        };
        let text = match cells.get(&a) {
            Some(t) => t.clone(),
            None => continue,
        };
        for r in (a.0 + 1)..=(b.0.min(a.0 + MAX_MERGE_ROWS)) {
            cells.entry((r, a.1)).or_insert_with(|| text.clone());
        }
    }
    if cells.is_empty() {
        return vec![];
    }
    let used: BTreeMap<i64, ()> = cells.keys().map(|(r, _)| (*r, ())).collect();
    let width = cells.keys().map(|(_, c)| *c).max().unwrap_or(0) + 1;
    used.keys().map(|r| (0..width).map(|col| pyutil::strip(cells.get(&(*r, col)).map(|s| s.as_str()).unwrap_or("")).to_string()).collect()).collect()
}

// 시트 → 빈 열로 갈라진 표들
fn sheet_tables(root: &Node, shared: &[String], styles: &[u8], warnings: &mut Vec<String>, name: &str) -> Vec<Vec<Vec<String>>> {
    let rows = sheet_rows(root, shared, styles, warnings, name);
    if rows.is_empty() {
        return vec![];
    }
    let width = rows[0].len();
    let empty: HashSet<usize> = (0..width).filter(|&j| !rows.iter().any(|r| !pyutil::strip(&r[j]).is_empty())).collect();
    let mut spans: Vec<[usize; 2]> = vec![];
    let mut start: Option<usize> = None;
    for j in 0..=width {
        if j < width && !empty.contains(&j) {
            if start.is_none() {
                start = Some(j);
            }
        } else if let Some(s) = start {
            spans.push([s, j]);
            start = None;
        }
    }
    let mut merged: Vec<[usize; 2]> = vec![];
    for s in spans {
        if s[1] - s[0] < MIN_TABLE_COLS && !merged.is_empty() {
            merged.last_mut().unwrap()[1] = s[1];
        } else {
            merged.push(s);
        }
    }
    if merged.len() > 1 && merged[0][1] - merged[0][0] < MIN_TABLE_COLS {
        merged[1][0] = merged[0][0];
        merged.remove(0);
    }
    let mut out = vec![];
    for [a, b] in merged {
        let grid: Vec<Vec<Option<String>>> = rows.iter().map(|r| r[a..b].iter().map(|c| Some(c.clone())).collect()).collect();
        let part = tables::finish(&grid);
        if !part.is_empty() {
            out.push(part);
        }
    }
    out
}

// 셀 메모(주석·스레드 댓글)
fn comments(z: &mut ooxml::Zip, names: &HashSet<String>) -> Vec<String> {
    let mut picked: Vec<(String, bool)> = vec![];
    let mut sorted_names: Vec<&String> = names.iter().collect();
    sorted_names.sort();
    for n in sorted_names {
        if COMMENTS.is_match(n) {
            picked.push((n.clone(), false));
        } else if TCOMMENTS.is_match(n) {
            picked.push((n.clone(), true));
        }
    }
    picked.sort_by_key(|(n, t)| ooxml::num_key(n, if *t { &TCOMMENTS } else { &COMMENTS }));
    let mut out = vec![];
    for (n, threaded) in picked {
        let root = match ooxml::parse(z, &n) {
            Some(r) => r,
            None => continue,
        };
        let (outer, inner) = if threaded { ("threadedComment", "text") } else { ("comment", "t") };
        for c in root.iter() {
            if c.tag != outer {
                continue;
            }
            let txt: String = c.iter().into_iter().filter(|e| e.tag == inner).map(|e| e.text.as_str()).collect();
            let t = pyutil::strip(&txt);
            if !t.is_empty() {
                out.push(t.to_string());
            }
        }
    }
    out
}
