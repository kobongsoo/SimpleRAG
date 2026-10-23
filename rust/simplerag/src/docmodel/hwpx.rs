//------------------------------------------------------------------
// HWPX → 블록 (Python docmodel/hwpx.py)
//=> 문단 모양(paraPr)의 heading 정보(OUTLINE = 개요 제목, NUMBER·BULLET = 목록)로 블록 종류를
//   정하고, 표(셀 주소·병합·머리글 표시), 머리말·꼬리말, 각주·미주를 읽는다.
//------------------------------------------------------------------

use std::collections::HashMap;

use once_cell::sync::Lazy;
use regex::Regex;

use super::blocks::{self as B, Block, Document};
use super::ooxml::{self, attr};
use super::tables;
use crate::pyutil;
use crate::xmldom::Node;

static SECTION: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)Contents/section(\d+)\.xml$").unwrap());

//------------------------------------------------------------------
// HWPX 추출 (진입점)
//------------------------------------------------------------------
pub fn extract(path: &str, title: &str) -> Result<Document, String> {
    let mut z = ooxml::open(path, "hwpx")?;
    let mut names: Vec<String> = ooxml::names(&z).into_iter().filter(|n| SECTION.is_match(n)).collect();
    names.sort_by_key(|n| ooxml::num_key(n, &SECTION));
    if names.is_empty() {
        return Err("hwpx 본문(Contents/section*.xml) 없음".into());
    }
    let kinds = para_kinds(ooxml::parse(&mut z, "Contents/header.xml").as_ref());
    let mut doc = Document::new("hwpx", title);
    let mut notes: Vec<String> = vec![];
    for n in &names {
        let root = match ooxml::parse(&mut z, n) {
            Some(r) => r,
            None => {
                doc.warnings.push(format!("{} XML 손상", n));
                continue;
            }
        };
        let mut w = Walker { kinds: &kinds, out: &mut doc.blocks, notes: &mut notes };
        for p in root.children.iter().filter(|p| p.tag == "p") {
            w.paragraph(p, None);
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

// 문단 모양 id → (heading|list, 수준)
fn para_kinds(root: Option<&Node>) -> HashMap<String, (&'static str, i64)> {
    let mut out = HashMap::new();
    let root = match root {
        Some(r) => r,
        None => return out,
    };
    for pp in root.iter() {
        if pp.tag != "paraPr" {
            continue;
        }
        let h = match pp.child("heading") {
            Some(h) => h,
            None => continue,
        };
        let t = h.get("type").unwrap_or("").to_uppercase();
        let lvl = h.get("level").and_then(|l| pyutil::strip(l).parse::<i64>().ok()).unwrap_or(0);
        let id = pp.get("id").unwrap_or("").to_string();
        if t == "OUTLINE" {
            out.insert(id, ("heading", lvl + 1));
        } else if t == "NUMBER" || t == "BULLET" {
            out.insert(id, ("list", lvl + 1));
        }
    }
    out
}

// 숫자 속성(실패하면 기본값)
fn int_or(v: Option<&str>, default: i64) -> i64 {
    v.and_then(|s| pyutil::strip(s).parse::<i64>().ok()).unwrap_or(default)
}

// 문단 훑개 — 블록과 각주를 모은다
struct Walker<'a> {
    kinds: &'a HashMap<String, (&'static str, i64)>,
    out: &'a mut Vec<Block>,
    notes: &'a mut Vec<String>,
}

impl<'a> Walker<'a> {
    //------------------------------------------------------------------
    // 문단 하나 — 런 안 글자·표·컨트롤·하위 목록(글상자 등)
    //------------------------------------------------------------------
    fn paragraph(&mut self, p: &Node, kind_out: Option<&'static str>) {
        let mut buf = String::new();
        let (kind, level) = p.get("paraPrIDRef").and_then(|id| self.kinds.get(id)).copied().map(|(k, l)| (Some(k), l)).unwrap_or((None, 0));
        let flush = |buf: &mut String, out: &mut Vec<Block>| {
            let s = std::mem::take(buf);
            if pyutil::strip(&s).is_empty() {
                return;
            }
            if let Some(k) = kind_out {
                out.push(Block::new(k, s));
            } else if kind == Some("heading") {
                out.push(Block::new(B::HEADING, s).lv(level));
            } else if kind == Some("list") {
                out.push(Block::new(B::LIST_ITEM, s).lv(level));
            } else {
                out.push(Block::new(B::PARAGRAPH, s));
            }
        };
        for run in p.children.iter().filter(|r| r.tag == "run") {
            for el in &run.children {
                match el.tag.as_str() {
                    "t" => t_text(el, &mut buf),
                    "tbl" => {
                        flush(&mut buf, self.out);
                        let (rows, nh) = self.table(el);
                        if !rows.is_empty() {
                            self.out.push(Block::table(rows, nh));
                        }
                    }
                    "ctrl" => self.ctrl(el),
                    "secPr" | "colPr" => continue,
                    _ => {
                        let lists = sublists(el);
                        if !lists.is_empty() {
                            flush(&mut buf, self.out);
                            for sl in lists {
                                for q in sl.children.iter().filter(|q| q.tag == "p") {
                                    self.paragraph(q, kind_out);
                                }
                            }
                        }
                    }
                }
            }
        }
        flush(&mut buf, self.out);
    }

    // 컨트롤 — 머리말·꼬리말, 각주·미주
    fn ctrl(&mut self, ctrl: &Node) {
        for el in &ctrl.children {
            match el.tag.as_str() {
                "header" | "footer" => {
                    let kind = if el.tag == "header" { B::HEADER } else { B::FOOTER };
                    for sl in sublists(el) {
                        for q in sl.children.iter().filter(|q| q.tag == "p") {
                            self.paragraph(q, Some(kind));
                        }
                    }
                }
                "footNote" | "endNote" => {
                    let mut tmp = vec![];
                    {
                        let mut w = Walker { kinds: self.kinds, out: &mut tmp, notes: self.notes };
                        for sl in sublists(el) {
                            for q in sl.children.iter().filter(|q| q.tag == "p") {
                                w.paragraph(q, None);
                            }
                        }
                    }
                    let s = tmp.iter().filter(|b| !b.text.is_empty()).map(|b| b.text.as_str()).collect::<Vec<_>>().join("\n");
                    if !pyutil::strip(&s).is_empty() {
                        self.notes.push(s);
                    }
                }
                _ => {}
            }
        }
    }

    // 표 → (행렬, 머리글 행 수)
    fn table(&mut self, tbl: &Node) -> (Vec<Vec<String>>, usize) {
        let mut cells = vec![];
        let mut n_rows = int_or(tbl.get("rowCnt"), 0);
        let mut n_cols = int_or(tbl.get("colCnt"), 0);
        let mut head_rows: HashMap<i64, bool> = HashMap::new();
        let mut auto_r = 0i64;
        for tr in tbl.children.iter().filter(|t| t.tag == "tr") {
            let mut auto_c = 0i64;
            for tc in tr.children.iter().filter(|t| t.tag == "tc") {
                let (addr, span) = (tc.child("cellAddr"), tc.child("cellSpan"));
                let r = int_or(attr(addr, "rowAddr"), auto_r);
                let c = int_or(attr(addr, "colAddr"), auto_c);
                let rs = int_or(attr(span, "rowSpan"), 1);
                let cs = int_or(attr(span, "colSpan"), 1);
                auto_c = c + cs;
                head_rows.entry(r).or_insert(true);
                if !matches!(tc.get("header"), Some("1") | Some("true")) {
                    head_rows.insert(r, false);
                }
                let text = self.cell_text(tc);
                cells.push((r, c, rs, cs, text));
                n_rows = n_rows.max(r + rs);
                n_cols = n_cols.max(c + cs);
            }
            auto_r += 1;
        }
        let mut grid = tables::new_grid(n_rows.max(0) as usize, n_cols.max(0) as usize);
        for (r, c, rs, cs, text) in cells {
            tables::place(&mut grid, r, c, &text, rs, cs);
        }
        let mut nh = 0i64;
        while head_rows.get(&nh).copied().unwrap_or(false) {
            nh += 1;
        }
        (tables::finish(&grid), nh as usize)
    }

    // 칸 글자 — 칸 안 문단을 따로 훑어 줄로 잇는다(중첩 표는 행을 한 줄로)
    fn cell_text(&mut self, tc: &Node) -> String {
        let mut tmp = vec![];
        {
            let mut w = Walker { kinds: self.kinds, out: &mut tmp, notes: self.notes };
            for sl in sublists(tc) {
                for q in sl.children.iter().filter(|q| q.tag == "p") {
                    w.paragraph(q, None);
                }
            }
        }
        let mut lines = vec![];
        for b in tmp {
            if b.kind == B::TABLE {
                for row in b.rows.unwrap_or_default() {
                    lines.push(row.into_iter().filter(|c| !c.is_empty()).collect::<Vec<_>>().join(" "));
                }
            } else if !b.text.is_empty() {
                lines.push(b.text);
            }
        }
        lines.join("\n")
    }
}

// <hp:t> 안 글자 — 탭·줄바꿈·특수 공백 요소를 글자로
fn t_text(t: &Node, buf: &mut String) {
    buf.push_str(&t.text);
    for sub in &t.children {
        match sub.tag.as_str() {
            "tab" => buf.push('\t'),
            "lineBreak" => buf.push('\n'),
            "fwSpace" | "nbSpace" | "hyphen" => buf.push(' '),
            _ => {}
        }
        buf.push_str(&sub.tail);
    }
}

// 아래로 내려가며 처음 만나는 subList 들(subList 안으로는 더 들어가지 않는다)
fn sublists(el: &Node) -> Vec<&Node> {
    let mut out = vec![];
    fn walk<'b>(e: &'b Node, out: &mut Vec<&'b Node>) {
        for ch in &e.children {
            if ch.tag == "subList" {
                out.push(ch);
            } else {
                walk(ch, out);
            }
        }
    }
    walk(el, &mut out);
    out
}
