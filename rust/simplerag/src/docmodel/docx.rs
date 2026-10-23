//------------------------------------------------------------------
// DOCX → 블록 (Python docmodel/ooxml_docx.py)
//=> 문단 스타일(제목 n·개요 수준·목차·캡션)과 번호 목록, 표(병합 포함), 머리말·꼬리말, 각주를 읽는다.
//------------------------------------------------------------------

use std::collections::HashMap;

use once_cell::sync::Lazy;
use regex::Regex;

use super::blocks::{self as B, Block, Document};
use super::ooxml::{self, attr, child, isdigit};
use super::tables;
use crate::pyutil;
use crate::xmldom::Node;

static HEADING_NAME: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^(?:heading|제목|개요)\s*(\d)$").unwrap());
static TITLE_NAME: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^(?:title|제목)$").unwrap());
static TOC_NAME: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^(?:toc|목차)\s*\d*$|^toc heading$").unwrap());
static CAPTION_NAME: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^(?:caption|캡션)$").unwrap());

//------------------------------------------------------------------
// 문단 안 런 글자 모으기 — 중첩 문단·mc:Fallback 은 건너뛴다, 줄바꿈 런은 "\n"
//------------------------------------------------------------------
pub fn para_runs(el: &Node, buf: &mut String, in_run: bool) {
    for ch in &el.children {
        match ch.tag.as_str() {
            "p" | "Fallback" => continue,
            "r" => para_runs(ch, buf, true),
            "t" if in_run => buf.push_str(&ch.text),
            "tab" if in_run => buf.push('\t'),
            "br" | "cr" if in_run => {
                if ch.get("type") != Some("page") {
                    buf.push('\n');
                }
            }
            _ => para_runs(ch, buf, in_run),
        }
    }
}

//------------------------------------------------------------------
// 문단 안에 든 글상자 등의 중첩 문단 글자들
//------------------------------------------------------------------
pub fn inner_paragraphs(p: &Node) -> Vec<String> {
    let mut out = vec![];
    fn walk(el: &Node, out: &mut Vec<String>) {
        for ch in &el.children {
            if ch.tag == "Fallback" {
                continue;
            }
            if ch.tag == "p" {
                let mut buf = String::new();
                para_runs(ch, &mut buf, false);
                if !pyutil::strip(&buf).is_empty() {
                    out.push(buf);
                }
            }
            walk(ch, out);
        }
    }
    for ch in &p.children {
        if ch.tag == "pPr" {
            continue;
        }
        walk(ch, &mut out);
    }
    out
}

// 스타일 표
struct Styles {
    info: HashMap<String, (String, Option<String>, Option<i64>)>,
    default: Option<String>,
}

impl Styles {
    fn new(root: Option<&Node>) -> Styles {
        let mut s = Styles { info: HashMap::new(), default: None };
        let root = match root {
            Some(r) => r,
            None => return s,
        };
        for st in &root.children {
            let ty = st.get("type");
            if st.tag != "style" || !(ty == Some("paragraph") || ty.is_none()) {
                continue;
            }
            let sid = st.get("styleId").unwrap_or("").to_string();
            let name = attr(child(Some(st), "name"), "val").unwrap_or("");
            let based = attr(child(Some(st), "basedOn"), "val").map(|s| s.to_string());
            let lvl = attr(child(child(Some(st), "pPr"), "outlineLvl"), "val");
            let lvl = lvl.filter(|l| isdigit(l)).and_then(|l| l.parse().ok());
            s.info.insert(sid.clone(), (pyutil::strip(name).to_string(), based, lvl));
            if matches!(st.get("default"), Some("1") | Some("true")) {
                s.default = Some(sid);
            }
        }
        s
    }

    // 스타일 id → (종류, 수준) — basedOn 을 최대 10단계 따라간다
    fn kind(&self, sid: Option<&str>) -> (Option<&'static str>, i64) {
        let mut sid: Option<String> = sid.filter(|s| !s.is_empty()).map(|s| s.to_string()).or_else(|| self.default.clone());
        for _ in 0..10 {
            let key = match &sid {
                Some(k) => k.clone(),
                None => return (None, 0),
            };
            let (name, based, lvl) = match self.info.get(&key) {
                Some(x) => x,
                None => return (None, 0),
            };
            if TOC_NAME.is_match(name) {
                return (Some("toc"), 0);
            }
            if CAPTION_NAME.is_match(name) {
                return (Some("caption"), 0);
            }
            if let Some(m) = HEADING_NAME.captures(name) {
                return (Some("heading"), m.get(1).unwrap().as_str().parse::<i64>().unwrap_or(1).max(1));
            }
            if TITLE_NAME.is_match(name) {
                return (Some("heading"), 1);
            }
            if let Some(l) = lvl {
                if *l < 9 {
                    return (Some("heading"), l + 1);
                }
            }
            sid = based.clone();
        }
        (None, 0)
    }
}

//------------------------------------------------------------------
// DOCX 추출 (진입점)
//------------------------------------------------------------------
pub fn extract(path: &str, title: &str) -> Result<Document, String> {
    let mut z = ooxml::open(path, "docx")?;
    let names = ooxml::names(&z);
    let body_root = ooxml::parse(&mut z, "word/document.xml").ok_or("docx 본문(word/document.xml) 없음·손상")?;
    let styles_root = ooxml::parse(&mut z, "word/styles.xml");
    let styles = Styles::new(styles_root.as_ref());
    let mut extras: Vec<String> = names.iter()
        .filter(|n| n.ends_with(".xml") && (n.starts_with("word/header") || n.starts_with("word/footer")))
        .cloned().collect();
    extras.sort();
    let extra_roots: Vec<(String, Option<Node>)> = extras.into_iter().map(|n| { let r = ooxml::parse(&mut z, &n); (n, r) }).collect();
    let foot_root = if names.iter().any(|n| n == "word/footnotes.xml") { ooxml::parse(&mut z, "word/footnotes.xml") } else { None };

    let mut doc = Document::new("docx", title);
    let body = body_root.child("body").ok_or("docx 본문(<w:body>) 없음")?;
    walk(body, &styles, &mut doc.blocks, false);
    for (n, root) in &extra_roots {
        let root = match root {
            Some(r) => r,
            None => continue,
        };
        let kind = if n.contains("/header") { B::HEADER } else { B::FOOTER };
        for p in root.iter() {
            if p.tag == "p" {
                let mut buf = String::new();
                para_runs(p, &mut buf, false);
                if !pyutil::strip(&buf).is_empty() {
                    doc.blocks.push(Block::new(kind, buf));
                }
            }
        }
    }
    let mut notes = vec![];
    if let Some(fr) = &foot_root {
        for fn_ in &fr.children {
            if fn_.tag != "footnote" || fn_.get("type").is_some() {
                continue;
            }
            let mut buf = String::new();
            for p in fn_.iter() {
                if p.tag == "p" {
                    para_runs(p, &mut buf, false);
                    buf.push('\n');
                }
            }
            let s = pyutil::strip(&buf).to_string();
            if !s.is_empty() {
                notes.push(s);
            }
        }
    }
    if !notes.is_empty() {
        let mut h = Block::new(B::HEADING, "각주").lv(1);
        h.synthetic = true;
        doc.blocks.push(h);
        doc.blocks.extend(notes.into_iter().map(|s| Block::new(B::NOTE, s)));
    }
    Ok(doc)
}

// 본문 훑기 — 문단·표·내용 컨트롤(sdt)
fn walk(el: &Node, styles: &Styles, out: &mut Vec<Block>, in_toc: bool) {
    for ch in &el.children {
        match ch.tag.as_str() {
            "p" => paragraph(ch, styles, out, in_toc),
            "tbl" => {
                let (rows, nh) = table(ch);
                if !rows.is_empty() {
                    out.push(Block::table(rows, nh));
                }
            }
            "sdt" => {
                let mut gallery = String::new();
                if let Some(pr) = ch.child("sdtPr") {
                    for e in pr.iter() {
                        if e.tag == "docPartGallery" {
                            gallery = e.get("val").unwrap_or("").to_string();
                        }
                    }
                }
                let target = ch.child("sdtContent").unwrap_or(ch);
                walk(target, styles, out, in_toc || gallery.to_lowercase().contains("table of contents"));
            }
            "sectPr" | "bookmarkStart" | "bookmarkEnd" | "proofErr" => continue,
            _ => walk(ch, styles, out, in_toc),
        }
    }
}

// 문단 하나 → 블록
fn paragraph(p: &Node, styles: &Styles, out: &mut Vec<Block>, in_toc: bool) {
    let mut text = String::new();
    para_runs(p, &mut text, false);
    let ppr = p.child("pPr");
    let (mut kind, mut level) = styles.kind(attr(child(ppr, "pStyle"), "val"));
    if let Some(d) = attr(child(ppr, "outlineLvl"), "val") {
        if isdigit(d) {
            if let Ok(n) = d.parse::<i64>() {
                if n < 9 {
                    kind = Some("heading");
                    level = n + 1;
                }
            }
        }
    }
    let numpr = child(ppr, "numPr");
    if !pyutil::strip(&text).is_empty() {
        if in_toc || kind == Some("toc") {
            out.push(Block::new(B::TOC, text));
        } else if kind == Some("heading") {
            out.push(Block::new(B::HEADING, text).lv(level));
        } else if kind == Some("caption") {
            out.push(Block::new(B::CAPTION, text));
        } else if numpr.is_some() {
            let ilvl = attr(child(numpr, "ilvl"), "val");
            let lv = ilvl.filter(|v| isdigit(v)).and_then(|v| v.parse::<i64>().ok()).map(|v| v + 1).unwrap_or(1);
            out.push(Block::new(B::LIST_ITEM, text).lv(lv));
        } else {
            out.push(Block::new(B::PARAGRAPH, text));
        }
    }
    for s in inner_paragraphs(p) {
        out.push(Block::new(if in_toc { B::TOC } else { B::PARAGRAPH }, s));
    }
}

//------------------------------------------------------------------
// 표 → (행렬, 머리글 행 수) — gridSpan(가로)·vMerge(세로) 반영
//------------------------------------------------------------------
fn table(tbl: &Node) -> (Vec<Vec<String>>, usize) {
    let trs: Vec<&Node> = tbl.children.iter().filter(|t| t.tag == "tr").collect();
    let mut width = tbl.child("tblGrid").map(|g| g.children.iter().filter(|c| c.tag == "gridCol").count()).unwrap_or(0) as i64;
    let mut raw: Vec<Vec<(i64, String, i64, Option<String>)>> = vec![];
    let mut header = 0usize;
    let mut counting = true;
    for tr in trs {
        let trpr = tr.child("trPr");
        let th = child(trpr, "tblHeader");
        let is_head = th.is_some() && !matches!(attr(th, "val"), Some("0") | Some("false"));
        if counting && is_head {
            header += 1;
        } else {
            counting = false;
        }
        let mut col = 0i64;
        if let Some(b) = attr(child(trpr, "gridBefore"), "val") {
            if isdigit(b) {
                col = b.parse().unwrap_or(0);
            }
        }
        let mut cells = vec![];
        for tc in tr.children.iter().filter(|c| c.tag == "tc") {
            let tcpr = tc.child("tcPr");
            let span = attr(child(tcpr, "gridSpan"), "val").filter(|s| isdigit(s)).and_then(|s| s.parse().ok()).unwrap_or(1i64);
            let vm = child(tcpr, "vMerge");
            let vmerge = vm.map(|v| v.get("val").unwrap_or("continue").to_string());
            cells.push((col, cell_text(tc), span, vmerge));
            col += span;
        }
        width = width.max(col);
        raw.push(cells);
    }
    let mut grid = tables::new_grid(raw.len(), width.max(0) as usize);
    for (r, cells) in raw.iter().enumerate() {
        for (col, text, span, vmerge) in cells {
            let mut text = text.clone();
            if vmerge.as_deref() == Some("continue") && r > 0 && *col < width {
                text = grid[r - 1].get(*col as usize).cloned().flatten().unwrap_or_default();
            }
            tables::place(&mut grid, r as i64, *col, &text, 1, *span);
        }
    }
    (tables::finish(&grid), header)
}

// 칸 글자(문단·중첩 표·내용 컨트롤)
fn cell_text(tc: &Node) -> String {
    let mut lines: Vec<String> = vec![];
    for ch in &tc.children {
        match ch.tag.as_str() {
            "p" => {
                let mut buf = String::new();
                para_runs(ch, &mut buf, false);
                lines.push(buf);
                lines.extend(inner_paragraphs(ch));
            }
            "tbl" => {
                let (rows, _) = table(ch);
                lines.extend(rows.into_iter().map(|r| r.into_iter().filter(|c| !c.is_empty()).collect::<Vec<_>>().join(" ")));
            }
            "sdt" => {
                if let Some(content) = ch.child("sdtContent") {
                    for p in &content.children {
                        if p.tag == "p" {
                            let mut buf = String::new();
                            para_runs(p, &mut buf, false);
                            lines.push(buf);
                        }
                    }
                }
            }
            _ => {}
        }
    }
    lines.into_iter().filter(|s| !pyutil::strip(s).is_empty()).collect::<Vec<_>>().join("\n")
}
