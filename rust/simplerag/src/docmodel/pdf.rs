//------------------------------------------------------------------
// PDF(텍스트층) → 블록 (Python docmodel/pdf.py)
//    · 쪽 경계 — 블록마다 page
//    · 머리말·꼬리말 — 쪽 맨 위·아래 2줄 가운데 3쪽 이상에서 되풀이되는 줄(숫자는 '#')
//    · 쪽번호 — "12" · "- 12 -" · "3 / 30" · "Page 3" 꼴
//    · 제목(P-2, 설정으로 켤 때만) — 본문보다 확연히 큰 짧은 줄
//    · 표(extract.pdf_tables, 기본 켬) — 테두리 선으로 표를 찾아(pdftable) TABLE 블록으로 두고
//      그 글자는 본문 줄에서 뺀다. 표 블록은 표가 시작된 줄 자리에 들어간다
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
    let want_tables = crate::config::cfg().pdf_tables;
    let mut stop_note: Option<(usize, usize)> = None;
    let (raw_pages, _n) = crate::pdfium::read_pages(path, infer, want_tables, |i, n| {
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
    let mut anchors: Vec<BTreeMap<usize, Vec<Vec<Vec<String>>>>> = vec![];
    for p in &raw_pages {
        // 표가 있으면 표 글자를 뺀 글로 줄을 만든다(줄 수는 원문과 같다)
        let (body, at) = if p.tables.is_empty() { (p.text.clone(), BTreeMap::new()) } else { strip_tables(&p.text, &p.tables) };
        let lines: Vec<String> = split_lines(&body).into_iter().map(|l| pyutil::rstrip(&l).to_string()).collect();
        // 글자 크기는 원문 글(p.text)로 잰다 — 줄바꿈을 그대로 두었으므로 줄 번호가 같다
        let sz = if infer { line_sizes(p, lines.len()) } else { vec![None; lines.len()] };
        pages.push(lines);
        sizes.push(sz);
        anchors.push(at);
    }
    doc.pages = Some(pages.len() as i64);
    let n_tables: usize = anchors.iter().map(|at| at.values().map(|v| v.len()).sum::<usize>()).sum();
    let total: usize = pages.iter().map(|p| p.iter().map(|l| l.chars().filter(|c| !pyutil::is_space(*c)).count()).sum::<usize>()).sum();
    // 글이 전부 표 안에 있는 쪽들도 텍스트층은 있는 것이다
    if total < MIN_TEXT_CHARS && n_tables == 0 {
        return Err("PDF 텍스트층 없음(스캔 PDF)".into());
    }
    let edge = edge_kinds(&pages);
    let heads = if infer { infer_headings(&pages, &sizes, &edge) } else { HashMap::new() };
    for (pi, (lines, at)) in pages.iter().zip(&anchors).enumerate() {
        let no = pi as i64 + 1;
        for (j, ln) in lines.iter().enumerate() {
            // 이 줄에서 시작한 표를 먼저 — 표 앞 글(표 제목·단위)은 앞 줄에 이미 나왔다
            if let Some(tabs) = at.get(&j) {
                for rows in tabs {
                    doc.blocks.push(Block::table(rows.clone(), 0).pg(Some(no)));
                }
            }
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

//------------------------------------------------------------------
// 쪽 글에서 표 글자 빼기 (Python pdf._strip_tables)
//=> 표 셀에 들어간 글자를 쪽 글에서 지운다(같은 글이 본문 줄과 표에 두 번 들어가지 않게).
//   줄바꿈은 그대로 두어 줄 번호가 원문과 같게 유지한다 — 글자 크기(line_sizes)와 맞추기 위해.
//   표마다 '첫 글자가 있던 줄'을 기록해 그 자리에 표 블록을 끼운다.
// -in: txt = 쪽 글 / tables = pdftable 결과
// -out: (표 글자를 뺀 글, {줄 번호: [표 행렬, …]})
//------------------------------------------------------------------
fn strip_tables(txt: &str, tables: &[super::pdftable::Table]) -> (String, BTreeMap<usize, Vec<Vec<Vec<String>>>>) {
    // 첫 글자 위치 → 표 (같은 위치면 뒤의 표가 이긴다 — 파이썬 dict 와 같다)
    let mut first: HashMap<usize, &Vec<Vec<String>>> = HashMap::new();
    let mut drop: std::collections::HashSet<usize> = std::collections::HashSet::new();
    for t in tables {
        if let Some(&m) = t.used.iter().next() {
            first.insert(m, &t.rows);
        }
        drop.extend(t.used.iter().copied());
    }
    let chars: Vec<char> = txt.chars().collect();
    let mut out = String::with_capacity(txt.len());
    let mut at: BTreeMap<usize, Vec<Vec<Vec<String>>>> = BTreeMap::new();
    let (mut line, mut i) = (0usize, 0usize);
    while i < chars.len() {
        let ch = chars[i];
        if let Some(rows) = first.get(&i) {
            at.entry(line).or_default().push((*rows).clone());
        }
        if ch == '\r' || ch == '\n' {
            out.push(ch);
            // \r\n 은 줄바꿈 하나 — split_lines 와 같은 규칙
            if ch == '\r' && i + 1 < chars.len() && chars[i + 1] == '\n' {
                out.push('\n');
                i += 1;
            }
            line += 1;
        } else if !drop.contains(&i) {
            out.push(ch);
        }
        i += 1;
    }
    (out, at)
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
