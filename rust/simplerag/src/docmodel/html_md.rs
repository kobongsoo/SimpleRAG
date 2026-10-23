//------------------------------------------------------------------
// HTML · Markdown · ipynb → 블록 (Python docmodel/html_md.py)
//------------------------------------------------------------------

use once_cell::sync::Lazy;
use regex::Regex;

use super::blocks::{self as B, Block, Document};
use super::tables;
use crate::htmlparse::{self, Ev};
use crate::pyutil;

//------------------------------------------------------------------
// 파일 글자 읽기 — utf-8-sig → cp949 → utf-8(대체)
//------------------------------------------------------------------
pub fn read_text(path: &str) -> Result<String, String> {
    let raw = std::fs::read(path).map_err(|e| e.to_string())?;
    let body = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&raw);
    if let Ok(s) = std::str::from_utf8(body) {
        return Ok(s.to_string());
    }
    let (s, _, bad) = encoding_rs::EUC_KR.decode(&raw);
    if !bad {
        return Ok(s.into_owned());
    }
    Ok(String::from_utf8_lossy(&raw).into_owned())
}

static MD_HEAD: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$").unwrap());
static MD_FENCE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s{0,3}(```|~~~)").unwrap());
static MD_TABLE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*\|.*\|\s*$").unwrap());
static MD_TABLE_SEP: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$").unwrap());
static MD_LIST: Lazy<Regex> = Lazy::new(|| Regex::new(r"^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(.*)$").unwrap());

//------------------------------------------------------------------
// 마크다운 글 → 블록 (제목·목록·표·코드 울타리·문단)
//------------------------------------------------------------------
pub fn md_blocks(text: &str) -> Vec<Block> {
    let mut out: Vec<Block> = vec![];
    let mut para: Vec<String> = vec![];
    let mut table: Vec<String> = vec![];
    let mut code: Option<Vec<String>> = None;
    fn flush_para(para: &mut Vec<String>, out: &mut Vec<Block>) {
        if !para.is_empty() {
            let s = pyutil::strip(&para.join("\n")).to_string();
            if !s.is_empty() {
                out.push(Block::new(B::PARAGRAPH, s));
            }
            para.clear();
        }
    }
    fn flush_table(table: &mut Vec<String>, out: &mut Vec<Block>) {
        if table.is_empty() {
            return;
        }
        let mut rows: Vec<Vec<String>> = vec![];
        let mut header = 0;
        for ln in table.iter() {
            if MD_TABLE_SEP.is_match(ln) {
                header = rows.len();
                continue;
            }
            let inner = pyutil::strip_chars(pyutil::strip(ln), "|");
            rows.push(inner.split('|').map(|c| pyutil::strip(c).to_string()).collect());
        }
        let width = rows.iter().map(|r| r.len()).max().unwrap_or(0);
        for r in rows.iter_mut() {
            r.resize(width, String::new());
        }
        if !rows.is_empty() {
            out.push(Block::table(rows, header));
        }
        table.clear();
    }
    let norm = text.replace("\r\n", "\n");
    for line in norm.split('\n') {
        if let Some(c) = code.as_mut() {
            if MD_FENCE.is_match(line) {
                out.push(Block::new(B::CODE, c.join("\n")));
                code = None;
            } else {
                c.push(line.to_string());
            }
            continue;
        }
        if MD_FENCE.is_match(line) {
            flush_para(&mut para, &mut out);
            flush_table(&mut table, &mut out);
            code = Some(vec![]);
            continue;
        }
        if MD_TABLE.is_match(line) || (!table.is_empty() && MD_TABLE_SEP.is_match(line)) {
            flush_para(&mut para, &mut out);
            table.push(line.to_string());
            continue;
        }
        flush_table(&mut table, &mut out);
        if let Some(m) = MD_HEAD.captures(line) {
            flush_para(&mut para, &mut out);
            let t = pyutil::strip(m.get(2).unwrap().as_str());
            let t = if t.is_empty() { m.get(1).unwrap().as_str() } else { t };
            out.push(Block::new(B::HEADING, t).lv(m.get(1).unwrap().as_str().len() as i64));
            continue;
        }
        if let Some(m) = MD_LIST.captures(line) {
            flush_para(&mut para, &mut out);
            let indent = pyutil::clen(m.get(1).unwrap().as_str()) as i64;
            out.push(Block::new(B::LIST_ITEM, m.get(2).unwrap().as_str()).lv(indent / 2 + 1));
            continue;
        }
        if pyutil::strip(line).is_empty() {
            flush_para(&mut para, &mut out);
            continue;
        }
        para.push(line.to_string());
    }
    if let Some(c) = code {
        out.push(Block::new(B::CODE, c.join("\n")));
    }
    flush_para(&mut para, &mut out);
    flush_table(&mut table, &mut out);
    out
}

pub fn extract_md(path: &str, title: &str) -> Result<Document, String> {
    let mut doc = Document::new("md", title);
    doc.blocks = md_blocks(&read_text(path)?);
    Ok(doc)
}

pub fn extract_ipynb(path: &str, title: &str) -> Result<Document, String> {
    let nb: serde_json::Value = serde_json::from_str(&read_text(path)?).map_err(|e| format!("JSONDecodeError: {}", e))?;
    let mut doc = Document::new("ipynb", title);
    if let Some(cells) = nb.get("cells").and_then(|c| c.as_array()) {
        for cell in cells {
            let body = crate::extract::cell_source(cell);
            if pyutil::strip(&body).is_empty() {
                continue;
            }
            if cell.get("cell_type").and_then(|v| v.as_str()) == Some("markdown") {
                doc.blocks.extend(md_blocks(&body));
            } else {
                doc.blocks.push(Block::new(B::CODE, pyutil::rstrip(&body)));
            }
        }
    }
    Ok(doc)
}

// ── HTML ───────────────────────────────────────────────

const SKIP: &[&str] = &["script", "style", "noscript", "template"];
const BLOCK: &[&str] = &["p", "div", "section", "article", "main", "aside", "blockquote", "pre",
    "dd", "dt", "figcaption", "address", "form", "fieldset", "br", "hr", "title"];

fn exclude_kind(tag: &str) -> Option<&'static str> {
    match tag {
        "nav" | "header" => Some(B::HEADER),
        "footer" => Some(B::FOOTER),
        _ => None,
    }
}
fn head_level(tag: &str) -> Option<i64> {
    match tag {
        "h1" => Some(1), "h2" => Some(2), "h3" => Some(3), "h4" => Some(4), "h5" => Some(5), "h6" => Some(6),
        _ => None,
    }
}

// 열린 표 하나
struct OpenTable {
    rows: Vec<Vec<(String, i64, i64, bool)>>,
    row: Option<Vec<(String, i64, i64, bool)>>,
    cell: Option<Vec<String>>,
    span: (i64, i64, bool),
}

// HTML → 블록 상태 기계(_HtmlBlocks)
struct HtmlBlocks {
    out: Vec<Block>,
    buf: String,
    kind: (&'static str, i64),
    skip: usize,
    exclude: Vec<&'static str>,
    tables: Vec<OpenTable>,
}

impl HtmlBlocks {
    fn flush(&mut self) {
        let s = pyutil::collapse_ws(&self.buf);
        self.buf.clear();
        if s.is_empty() {
            return;
        }
        if let Some(t) = self.tables.last_mut() {
            if let Some(c) = t.cell.as_mut() {
                c.push(s);
                return;
            }
        }
        let (mut kind, mut level) = self.kind;
        if let Some(k) = self.exclude.last() {
            kind = k;
            level = 0;
        }
        self.out.push(Block::new(kind, s).lv(level));
    }

    fn start(&mut self, tag: &str, attrs: &[(String, Option<String>)]) {
        if SKIP.contains(&tag) {
            self.skip += 1;
            return;
        }
        if self.skip > 0 {
            return;
        }
        let get = |k: &str| attrs.iter().find(|(n, _)| n == k).and_then(|(_, v)| v.clone());
        if let Some(k) = exclude_kind(tag) {
            self.flush();
            self.exclude.push(k);
        } else if let Some(l) = head_level(tag) {
            self.flush();
            self.kind = (B::HEADING, l);
        } else if tag == "li" {
            self.flush();
            self.kind = (B::LIST_ITEM, 1);
        } else if BLOCK.contains(&tag) {
            self.flush();
        } else if tag == "table" {
            self.flush();
            self.tables.push(OpenTable { rows: vec![], row: None, cell: None, span: (1, 1, false) });
        } else if tag == "tr" && !self.tables.is_empty() {
            if self.tables.last().unwrap().cell.is_some() {
                self.end("td");
            }
            if self.tables.last().unwrap().row.is_some() {
                self.end("tr");
            }
            self.tables.last_mut().unwrap().row = Some(vec![]);
        } else if (tag == "td" || tag == "th") && !self.tables.is_empty() {
            if self.tables.last().unwrap().cell.is_some() {
                self.end("td");
            }
            let t = self.tables.last_mut().unwrap();
            if t.row.is_none() {
                t.row = Some(vec![]);
            }
            t.cell = Some(vec![]);
            t.span = (span_int(get("rowspan")), span_int(get("colspan")), tag == "th");
        }
    }

    fn end(&mut self, tag: &str) {
        if SKIP.contains(&tag) {
            self.skip = self.skip.saturating_sub(1);
            return;
        }
        if self.skip > 0 {
            return;
        }
        if exclude_kind(tag).is_some() && !self.exclude.is_empty() {
            self.flush();
            self.exclude.pop();
        } else if head_level(tag).is_some() || tag == "li" {
            self.flush();
            self.kind = (B::PARAGRAPH, 0);
        } else if BLOCK.contains(&tag) {
            self.flush();
        } else if (tag == "td" || tag == "th") && self.tables.last().map(|t| t.cell.is_some()).unwrap_or(false) {
            self.flush();
            let t = self.tables.last_mut().unwrap();
            let (rs, cs, th) = t.span;
            let text = t.cell.take().unwrap().join("\n");
            t.row.get_or_insert_with(Vec::new).push((text, rs, cs, th));
        } else if tag == "tr" && self.tables.last().map(|t| t.row.is_some()).unwrap_or(false) {
            let t = self.tables.last_mut().unwrap();
            let r = t.row.take().unwrap();
            t.rows.push(r);
        } else if tag == "table" && !self.tables.is_empty() {
            self.flush();
            if self.tables.last().unwrap().cell.is_some() {
                self.end("td");
            }
            if self.tables.last().unwrap().row.is_some() {
                self.end("tr");
            }
            let t = self.tables.pop().unwrap();
            let (rows, nh) = html_grid(&t.rows);
            if rows.is_empty() {
                return;
            }
            if let Some(outer) = self.tables.last_mut() {
                if let Some(c) = outer.cell.as_mut() {
                    for r in rows {
                        c.push(r.into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" "));
                    }
                    return;
                }
            }
            self.out.push(Block::table(rows, nh));
        }
    }
}

fn span_int(v: Option<String>) -> i64 {
    v.and_then(|s| pyutil::strip(&s).parse::<i64>().ok()).map(|n| n.max(1)).unwrap_or(1)
}

// 병합을 반영해 격자로(_html_grid)
fn html_grid(raw: &[Vec<(String, i64, i64, bool)>]) -> (Vec<Vec<String>>, usize) {
    let mut taken = std::collections::HashSet::new();
    let mut placed = vec![];
    let mut width = 0i64;
    for (r, row) in raw.iter().enumerate() {
        let r = r as i64;
        let mut c = 0i64;
        for (text, rs, cs, _) in row {
            while taken.contains(&(r, c)) {
                c += 1;
            }
            placed.push((r, c, *rs, *cs, text.clone()));
            for dr in 0..*rs {
                for dc in 0..*cs {
                    taken.insert((r + dr, c + dc));
                }
            }
            c += cs;
        }
        width = width.max(c);
    }
    let mut grid = tables::new_grid(raw.len(), width as usize);
    for (r, c, rs, cs, text) in placed {
        tables::place(&mut grid, r, c, &text, rs, cs);
    }
    let mut nh = 0;
    while nh < raw.len() && !raw[nh].is_empty() && raw[nh].iter().all(|x| x.3) {
        nh += 1;
    }
    (tables::finish(&grid), nh)
}

pub fn extract_html(path: &str, title: &str) -> Result<Document, String> {
    let mut p = HtmlBlocks { out: vec![], buf: String::new(), kind: (B::PARAGRAPH, 0), skip: 0, exclude: vec![], tables: vec![] };
    for ev in htmlparse::tokenize(&read_text(path)?) {
        match ev {
            // HTMLParser 기본 handle_startendtag = 여는 처리 후 닫는 처리
            Ev::Start(t, a, self_close) => {
                p.start(&t, &a);
                if self_close {
                    p.end(&t);
                }
            }
            Ev::End(t) => p.end(&t),
            Ev::Data(d) => {
                if p.skip == 0 {
                    p.buf.push_str(&d);
                }
            }
        }
    }
    p.flush();
    let mut doc = Document::new("html", title);
    doc.blocks = p.out;
    Ok(doc)
}
