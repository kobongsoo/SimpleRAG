//------------------------------------------------------------------
// 블록 후처리 (Python docmodel/postprocess.py)
//    1) 정제 — 토크나이저를 죽이는 문자 제거
//    2) 표 정리 — 한 칸짜리 표(글상자)는 문단으로, 나머지는 셀을 한 줄로 다듬어 렌더링
//    3) 목차 표시 — 점선 리더 + 쪽번호 줄이 이어지면 toc(청크 제외)
//    4) 제목 폴백 — 파일 제목이 너무 적으면 정규식(structure)으로 추측
//    5) 무손실 검사 — 제외된 글자가 지나치게 많으면 평문 폴백
//------------------------------------------------------------------

use once_cell::sync::Lazy;
use regex::Regex;

use super::blocks::{self as B, Block, Document};
use super::tables;
use crate::extract::{expand_row, sanitize};
use crate::pyutil;
use crate::structure;

const MIN_PARSER_HEADINGS: usize = 3;
const MIN_KEPT_RATIO: f64 = 0.7;
static TOC_LINE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:\.{3,}|·{3,}|…{2,}|-{4,}|_{4,}|\s{6,})\s*\d{1,4}\s*$").unwrap());
const MIN_TOC_RUN: usize = 3;
const NO_REGEX_FORMATS: &[&str] = &["pptx", "xlsx"];
const STACKED_ROW_FORMATS: &[&str] = &["doc"];

fn para_like(k: &str) -> bool {
    k == B::PARAGRAPH || k == B::LIST_ITEM
}

//------------------------------------------------------------------
// 후처리 진입점 — 문서를 제자리에서 바꾼다
//------------------------------------------------------------------
pub fn finalize(doc: &mut Document) {
    if !doc.is_blocks() {
        return;
    }
    sanitize_doc(doc);
    tidy_tables(doc);
    // 빈 블록은 합성 제목만 남긴다
    doc.blocks.retain(|b| b.synthetic || !pyutil::strip(&b.text).is_empty()
        || (b.kind == B::TABLE && b.rows.as_ref().map(|r| !r.is_empty()).unwrap_or(false)));
    mark_toc(doc);
    heading_fallback(doc);
    lossless_check(doc);
}

// 1) 정제
fn sanitize_doc(doc: &mut Document) {
    for b in doc.blocks.iter_mut() {
        if !b.text.is_empty() {
            b.text = sanitize(&b.text).0;
        }
        if let Some(rows) = b.rows.as_mut() {
            for row in rows.iter_mut() {
                for c in row.iter_mut() {
                    if !c.is_empty() {
                        *c = sanitize(c).0;
                    }
                }
            }
        }
    }
}

// 2) 표 정리
fn tidy_tables(doc: &mut Document) {
    let fmt = doc.format.clone();
    let mut out = vec![];
    for mut b in std::mem::take(&mut doc.blocks) {
        if b.kind != B::TABLE {
            out.push(b);
            continue;
        }
        let rows = b.rows.take().unwrap_or_default();
        if rows.is_empty() {
            continue;
        }
        let width = rows.iter().map(|r| r.len()).max().unwrap_or(0);
        if width <= 1 {
            // 한 칸짜리 표 — 셀 안 줄을 문단으로 펼친다
            for row in &rows {
                let cell = row.first().map(|s| s.as_str()).unwrap_or("");
                for line in cell.split('\n') {
                    let t = pyutil::strip(line);
                    if !t.is_empty() {
                        out.push(Block::new(B::PARAGRAPH, t).pg(b.page).src(b.source));
                    }
                }
            }
            continue;
        }
        let mut rows = rows;
        if STACKED_ROW_FORMATS.contains(&fmt.as_str()) {
            let (r, h) = expand_stacked(rows, b.header_rows);
            rows = r;
            b.header_rows = h;
        }
        let cleaned: Vec<Vec<String>> = rows.iter().map(|r| r.iter().map(|c| tables::clean_cell(c)).collect()).collect();
        b.header_rows = tables::header_rows(&cleaned, b.header_rows);
        b.text = tables::render(&cleaned);
        b.rows = Some(cleaned);
        out.push(b);
    }
    doc.blocks = out;
}

// 눕힌 행 펼치기(DOC 표)
fn expand_stacked(rows: Vec<Vec<String>>, n_header: usize) -> (Vec<Vec<String>>, usize) {
    let mut out = vec![];
    let mut new_header = 0;
    for (i, row) in rows.into_iter().enumerate() {
        let stacked = row.iter().filter(|c| c.split('\n').count() >= 3).count();
        let expanded = if stacked >= 2 { expand_row(&row) } else { vec![] };
        let part = if expanded.is_empty() { vec![row] } else { expanded };
        if i < n_header {
            new_header += part.len();
        }
        out.extend(part);
    }
    (out, new_header)
}

// 3) 목차 표시
fn mark_toc(doc: &mut Document) {
    let mut run: Vec<usize> = vec![];
    let close = |run: &mut Vec<usize>, blocks: &mut [Block]| {
        if run.len() >= MIN_TOC_RUN {
            for &i in run.iter() {
                blocks[i].kind = B::TOC;
            }
        }
        run.clear();
    };
    for i in 0..doc.blocks.len() {
        let b = &doc.blocks[i];
        if (para_like(b.kind) || b.kind == B::HEADING) && !b.synthetic && TOC_LINE.is_match(pyutil::strip(&b.text)) {
            run.push(i);
            continue;
        }
        close(&mut run, &mut doc.blocks);
    }
    close(&mut run, &mut doc.blocks);
}

// 4) 제목 폴백
fn heading_fallback(doc: &mut Document) {
    if NO_REGEX_FORMATS.contains(&doc.format.as_str()) {
        return;
    }
    let n = doc.blocks.len();
    let parser_heads: Vec<usize> = (0..n).filter(|&i| {
        let b = &doc.blocks[i];
        b.kind == B::HEADING && !b.synthetic && b.source == B::SRC_PARSER
    }).collect();
    if parser_heads.len() >= MIN_PARSER_HEADINGS {
        return;
    }
    let mut head_ids: std::collections::HashSet<usize> = parser_heads.into_iter().collect();
    let inferred: Vec<usize> = (0..n).filter(|&i| {
        let b = &doc.blocks[i];
        b.kind == B::HEADING && !b.synthetic && b.source == B::SRC_INFERRED
    }).collect();
    let mut base = 0;
    if inferred.len() >= MIN_PARSER_HEADINGS {
        base = inferred.iter().map(|&i| doc.blocks[i].level.max(1)).max().unwrap_or(0);
    } else {
        head_ids.extend(inferred);
    }
    let text = (0..n).filter(|&i| para_like(doc.blocks[i].kind) || head_ids.contains(&i))
        .map(|i| doc.blocks[i].text.as_str()).collect::<Vec<_>>().join("\n");
    let g = structure::detect_grammar(&text);
    let n_app = structure::appendix_count(&text);
    if g.is_none() && n_app < structure::MIN_APPENDIX {
        return;
    }
    let offset = if g.is_some() && n_app > 0 { 1 } else { 0 };
    let mut out = vec![];
    for (i, mut b) in std::mem::take(&mut doc.blocks).into_iter().enumerate() {
        if !para_like(b.kind) && !head_ids.contains(&i) {
            out.push(b);
            continue;
        }
        let (first, _, rest) = pyutil::partition(&b.text, "\n");
        let (first, rest) = (first.to_string(), rest.to_string());
        let level = regex_level(g, &first, offset).map(|l| (l + base).min(9));
        match level {
            None => {
                if head_ids.contains(&i) {
                    b.kind = B::PARAGRAPH;
                    b.level = 0;
                }
                out.push(b);
            }
            Some(lv) => {
                out.push(Block::new(B::HEADING, pyutil::strip(&first)).lv(lv).pg(b.page).src(B::SRC_REGEX));
                if !pyutil::strip(&rest).is_empty() {
                    out.push(Block::new(b.kind, pyutil::strip(&rest)).lv(b.level).pg(b.page).src(b.source));
                }
            }
        }
    }
    doc.blocks = out;
}

// 정규식 제목 줄의 수준
fn regex_level(g: Option<(&'static str, &'static Regex)>, line: &str, offset: i64) -> Option<i64> {
    static NUM: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*(\d+(?:\.\d+)*)").unwrap());
    let s = pyutil::rstrip(line);
    if let Some((name, pat)) = g {
        if structure::grammar_fullmatch(pat, s) {
            if name == "마크다운" {
                let st = pyutil::strip(s);
                let hashes = pyutil::clen(st) - pyutil::clen(pyutil::lstrip_chars(st, "#"));
                return Some((hashes as i64 + offset).min(9));
            }
            if name == "번호절" {
                let depth = NUM.captures(s).map(|c| c.get(1).unwrap().as_str().split('.').count()).unwrap_or(1);
                return Some((depth as i64 + offset).min(9));
            }
            return Some(1 + offset);
        }
    }
    if structure::appendix_fullmatch(s) {
        return Some(1);
    }
    None
}

// 5) 무손실 검사
fn lossless_check(doc: &mut Document) {
    let size = |b: &Block| -> usize {
        if b.synthetic { 0 } else { b.text.chars().filter(|c| !pyutil::is_space(*c)).count() }
    };
    let total: usize = doc.blocks.iter().map(size).sum();
    if total == 0 {
        doc.raw_text = Some(String::new());
        return;
    }
    let kept: usize = doc.blocks.iter().filter(|b| !B::excluded(b.kind)).map(size).sum();
    if (kept as f64) < total as f64 * MIN_KEPT_RATIO {
        doc.warnings.push(format!("제외 블록이 본문의 {:.0}% — 표시 오류로 보고 평문 폴백",
                                  (1.0 - kept as f64 / total as f64) * 100.0));
        doc.raw_text = Some(doc.full_text());
    }
}
