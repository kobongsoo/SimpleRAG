//------------------------------------------------------------------
// PDF(텍스트층) → 블록 (Python docmodel/pdf.py)
//    · 쪽 경계 — 블록마다 page
//    · 머리말·꼬리말 — 쪽 맨 위·아래 2줄 가운데 3쪽 이상에서 되풀이되는 줄(숫자는 '#')
//    · 쪽번호 — "12" · "- 12 -" · "3 / 30" · "Page 3" 꼴
//    · 제목(P-2, 설정으로 켤 때만) — 본문보다 확연히 큰 짧은 줄
//   한 줄이 블록 하나다. 텍스트층이 사실상 없으면 오류 → 기존 추출로 폴백.
//------------------------------------------------------------------

use std::collections::{BTreeMap, HashMap};

use once_cell::sync::Lazy;
use regex::Regex;

use super::blocks::{self as B, Block, Document};
use crate::pyutil;

const MAX_PAGES: usize = 3000;
const MAX_SECONDS: u64 = 120;
const EDGE_LINES: usize = 2;
const MIN_REPEAT_PAGES: usize = 3;
const MAX_HEADER_CHARS: usize = 60;
const MIN_HEADER_LETTERS: usize = 4;
const MIN_TEXT_CHARS: usize = 20;
const HEAD_RATIO: f64 = 1.12;
const HEAD_MAX_CHARS: usize = 60;
const MIN_INFERRED: usize = 3;
const HEAD_MAX_SHARE: f64 = 0.15;
const MAX_LEVEL: i64 = 3;

static PAGE_NO: Lazy<Regex> = Lazy::new(|| {
    Regex::new(concat!(r"(?i)^\s*(?:[-–—]\s*)?\d{1,4}(?:\s*[-–—])?\s*$",
                       r"|^\s*\d{1,4}\s*/\s*\d{1,4}\s*$",
                       r"|^\s*(?:page|p\.?)\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$")).unwrap()
});
static TOC_TAIL: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:\.{3,}|·{3,}|…{2,}|-{4,})\s*\d{1,4}\s*$").unwrap());
static WS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());
static DIGITS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d+").unwrap());
// 파이썬 [#\W_]+ — '#', 글자·숫자가 아닌 것, 밑줄
static NONWORD: Lazy<Regex> = Lazy::new(|| Regex::new(r"[#\W_]+").unwrap());
static HANGUL_OR_LATIN: Lazy<Regex> = Lazy::new(|| Regex::new(r"[가-힣A-Za-z]").unwrap());

// \r\n | \r | \n 으로 줄 나누기(_LINE_SPLIT)
fn split_lines(s: &str) -> Vec<String> {
    let mut out = vec![];
    let mut cur = String::new();
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c == '\r' {
            if it.peek() == Some(&'\n') {
                it.next();
            }
            out.push(std::mem::take(&mut cur));
        } else if c == '\n' {
            out.push(std::mem::take(&mut cur));
        } else {
            cur.push(c);
        }
    }
    out.push(cur);
    out
}

//------------------------------------------------------------------
// PDF 추출 (진입점)
// -out: Ok(Document) / Err("PDF 열기 실패" · "PDF 텍스트층 없음(스캔 PDF)")
//------------------------------------------------------------------
pub fn extract(path: &str, title: &str) -> Result<Document, String> {
    let mut doc = Document::new("pdf", title);
    let t0 = std::time::Instant::now();
    let infer = crate::config::cfg().pdf_infer_headings;
    let mut stop_note: Option<(usize, usize)> = None;
    let (raw_pages, _n) = crate::pdfium::read_pages(path, infer, |i, n| {
        if i >= MAX_PAGES || t0.elapsed().as_secs() > MAX_SECONDS {
            stop_note = Some((i, n));
            return false;
        }
        true
    }).map_err(|e| if e.contains("적재") { e } else { "PDF 열기 실패: PdfiumError".to_string() })?;
    if let Some((i, n)) = stop_note {
        doc.warnings.push(format!("PDF 부분 추출 {}/{}쪽 (상한)", i, n));
    }
    let mut pages: Vec<Vec<String>> = vec![];
    let mut sizes: Vec<Vec<Option<f64>>> = vec![];
    for p in &raw_pages {
        let lines: Vec<String> = split_lines(&p.text).into_iter().map(|l| pyutil::rstrip(&l).to_string()).collect();
        let sz = if infer { line_sizes(p, lines.len()) } else { vec![None; lines.len()] };
        pages.push(lines);
        sizes.push(sz);
    }
    doc.pages = Some(pages.len() as i64);
    let total: usize = pages.iter().map(|p| p.iter().map(|l| l.chars().filter(|c| !pyutil::is_space(*c)).count()).sum::<usize>()).sum();
    if total < MIN_TEXT_CHARS {
        return Err("PDF 텍스트층 없음(스캔 PDF)".into());
    }
    let edge = edge_kinds(&pages);
    let heads = if infer { infer_headings(&pages, &sizes, &edge) } else { HashMap::new() };
    for (pi, lines) in pages.iter().enumerate() {
        let no = pi as i64 + 1;
        for (j, ln) in lines.iter().enumerate() {
            let t = pyutil::strip(ln);
            if t.is_empty() {
                continue;
            }
            if let Some(lv) = heads.get(&(no, j)) {
                doc.blocks.push(Block::new(B::HEADING, t).lv(*lv).pg(Some(no)).src(B::SRC_INFERRED));
                continue;
            }
            let kind = edge.get(&(no, j)).copied().unwrap_or(B::PARAGRAPH);
            doc.blocks.push(Block::new(kind, t).pg(Some(no)));
        }
    }
    Ok(doc)
}

// 줄마다 글꼴 크기 중앙값(P-2). 글자 수와 글 길이가 다르면 그 쪽은 모름
fn line_sizes(p: &crate::pdfium::PageText, n_lines: usize) -> Vec<Option<f64>> {
    let sizes = match &p.sizes {
        Some(s) => s,
        None => return vec![None; n_lines],
    };
    let chars: Vec<char> = p.text.chars().collect();
    if p.n_chars as usize != chars.len() {
        return vec![None; n_lines];
    }
    let median = |v: &mut Vec<f64>| -> Option<f64> {
        if v.is_empty() {
            return None;
        }
        v.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let m = v.len();
        Some(if m % 2 == 1 { v[m / 2] } else { (v[m / 2 - 1] + v[m / 2]) / 2.0 })
    };
    let mut out = vec![];
    let mut cur: Vec<f64> = vec![];
    let mut i = 0;
    while i < chars.len() {
        let ch = chars[i];
        if ch == '\r' || ch == '\n' {
            out.push(median(&mut cur));
            cur.clear();
            i += if ch == '\r' && i + 1 < chars.len() && chars[i + 1] == '\n' { 2 } else { 1 };
            continue;
        }
        if !pyutil::is_space(ch) {
            cur.push(pyutil::round_to(sizes.get(i).copied().unwrap_or(0.0), 1));
        }
        i += 1;
    }
    out.push(median(&mut cur));
    if out.len() == n_lines { out } else { vec![None; n_lines] }
}

// 글자 크기로 제목 줄 추정(P-2)
fn infer_headings(pages: &[Vec<String>], sizes: &[Vec<Option<f64>>], edge: &HashMap<(i64, usize), &'static str>)
                  -> HashMap<(i64, usize), i64> {
    let key = |f: f64| (f * 10.0).round() as i64; // 0.1 단위 크기를 정수 키로
    let mut weight: BTreeMap<i64, usize> = BTreeMap::new();
    let mut n_lines = 0usize;
    for (lines, szs) in pages.iter().zip(sizes) {
        for (ln, s) in lines.iter().zip(szs) {
            let t = pyutil::strip(ln);
            if let Some(s) = s {
                if *s != 0.0 && pyutil::clen(t) >= 15 {
                    *weight.entry(key(*s)).or_default() += pyutil::clen(t);
                }
            }
            if !t.is_empty() {
                n_lines += 1;
            }
        }
    }
    if weight.is_empty() {
        return HashMap::new();
    }
    // 최빈(가중) 크기 — 같으면 먼저 나온 것(파이썬 dict 순서)에 가깝게 작은 키
    let body = *weight.iter().max_by(|a, b| a.1.cmp(b.1).then(b.0.cmp(a.0))).unwrap().0 as f64 / 10.0;
    let mut cand: Vec<((i64, usize), f64)> = vec![];
    for (pi, (lines, szs)) in pages.iter().zip(sizes).enumerate() {
        let no = pi as i64 + 1;
        for (j, (ln, s)) in lines.iter().zip(szs).enumerate() {
            let t = pyutil::strip(ln);
            let s = match s { Some(s) if *s != 0.0 => *s, _ => continue };
            if t.is_empty() || edge.contains_key(&(no, j)) || pyutil::clen(t) > HEAD_MAX_CHARS {
                continue;
            }
            if s >= body * HEAD_RATIO && HANGUL_OR_LATIN.is_match(t) && !TOC_TAIL.is_match(t) {
                cand.push(((no, j), s));
            }
        }
    }
    if cand.len() < MIN_INFERRED || cand.len() as f64 > n_lines as f64 * HEAD_MAX_SHARE {
        return HashMap::new();
    }
    let rk = |s: f64| (pyutil::round_to(s / body, 1) * 10.0).round() as i64;
    let mut ranks: Vec<i64> = cand.iter().map(|(_, s)| rk(*s)).collect();
    ranks.sort_unstable_by(|a, b| b.cmp(a));
    ranks.dedup();
    cand.into_iter().map(|(pos, s)| {
        let k = ranks.iter().position(|r| *r == rk(s)).unwrap_or(0) as i64;
        (pos, (k + 1).min(MAX_LEVEL))
    }).collect()
}

//------------------------------------------------------------------
// 쪽 가장자리 줄 종류 (머리말·꼬리말·쪽번호)
//------------------------------------------------------------------
fn edge_kinds(pages: &[Vec<String>]) -> HashMap<(i64, usize), &'static str> {
    let mut out: HashMap<(i64, usize), &'static str> = HashMap::new();
    // 키 순서를 처음 나온 순서로 지키려고 Vec 로 모은다
    let mut tops: Vec<(String, Vec<(i64, usize)>)> = vec![];
    let mut bottoms: Vec<(String, Vec<(i64, usize)>)> = vec![];
    for (pi, lines) in pages.iter().enumerate() {
        let no = pi as i64 + 1;
        let idx: Vec<usize> = (0..lines.len()).filter(|&j| !pyutil::strip(&lines[j]).is_empty()).collect();
        let top: Vec<usize> = idx.iter().take(EDGE_LINES).copied().collect();
        let bottom: Vec<usize> = idx.iter().skip(idx.len().saturating_sub(EDGE_LINES)).copied().collect();
        for (zone, picks) in [("top", top), ("bottom", bottom)] {
            for j in picks {
                let s = pyutil::strip(&lines[j]);
                if PAGE_NO.is_match(s) {
                    out.insert((no, j), B::PAGE_NUMBER);
                    continue;
                }
                let key = DIGITS.replace_all(&WS.replace_all(s, " "), "#").to_string();
                if pyutil::clen(s) > MAX_HEADER_CHARS || pyutil::clen(&NONWORD.replace_all(&key, "")) < MIN_HEADER_LETTERS {
                    continue;
                }
                let bucket = if zone == "top" { &mut tops } else { &mut bottoms };
                match bucket.iter_mut().find(|(k, _)| *k == key) {
                    Some((_, v)) => v.push((no, j)),
                    None => bucket.push((key, vec![(no, j)])),
                }
            }
        }
    }
    for (bucket, kind) in [(&tops, B::HEADER), (&bottoms, B::FOOTER)] {
        for (_, spots) in bucket.iter() {
            let pages_seen: std::collections::HashSet<i64> = spots.iter().map(|s| s.0).collect();
            if pages_seen.len() >= MIN_REPEAT_PAGES {
                let mut uniq = spots.clone();
                uniq.sort();
                uniq.dedup();
                for spot in uniq.into_iter().skip(1) {
                    out.entry(spot).or_insert(kind);
                }
            }
        }
    }
    out
}
