//------------------------------------------------------------------
// PPTX → 블록 (Python docmodel/ooxml_pptx.py)
//=> 슬라이드 하나가 절 하나다 — 제목 자리표시자(title·ctrTitle)가 제목, 없으면 합성 제목
//   "슬라이드 n". 본문 문단(들여쓰기 수준이 있으면 목록), 표, 쪽번호·날짜·바닥글, 발표자 노트.
//------------------------------------------------------------------

use once_cell::sync::Lazy;
use regex::Regex;

use super::blocks::{self as B, Block, Document};
use super::ooxml::{self, attr, child, isdigit};
use super::tables;
use crate::pyutil;
use crate::xmldom::Node;

static SLIDE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)ppt/slides/slide(\d+)\.xml$").unwrap());

fn foot_kind(ph: &str) -> Option<&'static str> {
    match ph {
        "sldNum" => Some(B::PAGE_NUMBER),
        "dt" | "ftr" => Some(B::FOOTER),
        "hdr" => Some(B::HEADER),
        _ => None,
    }
}

//------------------------------------------------------------------
// PPTX 추출 (진입점)
//------------------------------------------------------------------
pub fn extract(path: &str, title: &str) -> Result<Document, String> {
    let mut z = ooxml::open(path, "pptx")?;
    let slides = slide_order(&mut z);
    if slides.is_empty() {
        return Err("pptx 슬라이드 없음".into());
    }
    let mut doc = Document::new("pptx", title);
    doc.pages = Some(slides.len() as i64);
    let mut notes: Vec<(i64, Vec<String>)> = vec![];
    for (i, part) in slides.iter().enumerate() {
        let no = i as i64 + 1;
        let root = match ooxml::parse(&mut z, part) {
            Some(r) => r,
            None => {
                doc.warnings.push(format!("슬라이드 {} XML 손상", no));
                continue;
            }
        };
        slide(&root, no, &mut doc.blocks);
        for (_, (kind, target)) in ooxml::rels(&mut z, part) {
            if kind == "notesSlide" {
                let texts = ooxml::parse(&mut z, &target).map(|n| note_texts(&n)).unwrap_or_default();
                if !texts.is_empty() {
                    notes.push((no, texts));
                }
            }
        }
    }
    if !notes.is_empty() {
        let mut h = Block::new(B::HEADING, "발표자 노트").lv(1);
        h.synthetic = true;
        doc.blocks.push(h);
        for (no, texts) in notes {
            let mut h2 = Block::new(B::HEADING, format!("슬라이드 {}", no)).lv(2).pg(Some(no));
            h2.synthetic = true;
            doc.blocks.push(h2);
            doc.blocks.extend(texts.into_iter().map(|t| Block::new(B::NOTE, t).pg(Some(no))));
        }
    }
    Ok(doc)
}

// 슬라이드 순서 — presentation.xml 의 sldId 순서, 없으면 파일 이름 번호 순
fn slide_order(z: &mut ooxml::Zip) -> Vec<String> {
    let names: std::collections::HashSet<String> = ooxml::names(z).into_iter().collect();
    if let Some(pres) = ooxml::parse(z, "ppt/presentation.xml") {
        let rmap = ooxml::rels(z, "ppt/presentation.xml");
        let mut order = vec![];
        for e in pres.iter_tag("sldId") {
            let rid = e.attrs.iter().find(|(k, v)| k.rsplit(':').next() == Some("id") && rmap.iter().any(|(id, _)| id == v));
            if let Some((_, rid)) = rid {
                if let Some((_, (_, full))) = rmap.iter().find(|(id, _)| id == rid) {
                    if names.contains(full) {
                        order.push(full.clone());
                    }
                }
            }
        }
        if !order.is_empty() {
            return order;
        }
    }
    let mut v: Vec<String> = names.into_iter().filter(|n| SLIDE.is_match(n)).collect();
    v.sort_by_key(|n| ooxml::num_key(n, &SLIDE));
    v
}

// 슬라이드 하나 → 제목 + 본문 블록
fn slide(root: &Node, no: i64, out: &mut Vec<Block>) {
    let mut body = vec![];
    let mut title_text: Vec<String> = vec![];
    if let Some(tree) = root.iter().into_iter().find(|e| e.tag == "spTree") {
        shapes(tree, no, &mut body, &mut title_text);
    }
    let head = pyutil::strip(&title_text.iter().filter(|t| !pyutil::strip(t).is_empty()).cloned().collect::<Vec<_>>().join(" ")).to_string();
    if !head.is_empty() {
        out.push(Block::new(B::HEADING, head).lv(1).pg(Some(no)));
    } else {
        let mut h = Block::new(B::HEADING, format!("슬라이드 {}", no)).lv(1).pg(Some(no));
        h.synthetic = true;
        out.push(h);
    }
    out.extend(body);
}

// 도형 자리표시자 종류(ph type, 없으면 body)
fn ph_type(sh: &Node) -> Option<String> {
    sh.iter().into_iter().find(|e| e.tag == "ph").map(|e| e.get("type").unwrap_or("body").to_string())
}

fn shapes(tree: &Node, no: i64, body: &mut Vec<Block>, title_text: &mut Vec<String>) {
    for sh in &tree.children {
        match sh.tag.as_str() {
            "grpSp" => shapes(sh, no, body, title_text),
            "sp" => {
                let ph = ph_type(sh);
                let paras = paragraphs(sh.child("txBody"));
                if paras.is_empty() {
                    continue;
                }
                let ph_s = ph.as_deref().unwrap_or("");
                if ph_s == "title" || ph_s == "ctrTitle" {
                    title_text.extend(paras.into_iter().map(|(t, _)| t));
                } else if let Some(k) = foot_kind(ph_s) {
                    body.extend(paras.into_iter().map(|(t, _)| Block::new(k, t).pg(Some(no))));
                } else {
                    for (t, lvl) in paras {
                        let kind = if lvl > 0 { B::LIST_ITEM } else { B::PARAGRAPH };
                        body.push(Block::new(kind, t).lv(if lvl > 0 { lvl } else { 0 }).pg(Some(no)));
                    }
                }
            }
            "graphicFrame" => {
                for tbl in sh.iter_tag("tbl") {
                    let rows = table(tbl);
                    if !rows.is_empty() {
                        body.push(Block::table(rows, 0).pg(Some(no)));
                    }
                }
            }
            _ => {}
        }
    }
}

// 텍스트 상자 문단 → (글자, 들여쓰기 수준)
fn paragraphs(txbody: Option<&Node>) -> Vec<(String, i64)> {
    let mut out = vec![];
    let tb = match txbody {
        Some(t) => t,
        None => return out,
    };
    for p in &tb.children {
        if p.tag != "p" {
            continue;
        }
        let mut buf = String::new();
        for e in p.iter() {
            if e.tag == "t" {
                buf.push_str(&e.text);
            } else if e.tag == "br" {
                buf.push('\n');
            }
        }
        if !pyutil::strip(&buf).is_empty() {
            let lvl = attr(child(Some(p), "pPr"), "lvl").filter(|l| isdigit(l)).and_then(|l| l.parse().ok()).unwrap_or(0);
            out.push((buf, lvl));
        }
    }
    out
}

// 표 — hMerge 는 빈칸, vMerge 는 윗칸 글자 되풀이
fn table(tbl: &Node) -> Vec<Vec<String>> {
    let trs: Vec<&Node> = tbl.children.iter().filter(|t| t.tag == "tr").collect();
    let width = trs.iter().map(|tr| tr.children.iter().filter(|c| c.tag == "tc").count()).max().unwrap_or(0);
    let mut grid = tables::new_grid(trs.len(), width);
    for (r, tr) in trs.iter().enumerate() {
        let mut c = 0;
        for tc in tr.children.iter().filter(|c| c.tag == "tc") {
            let mut text = paragraphs(tc.child("txBody")).into_iter().map(|(t, _)| t).collect::<Vec<_>>().join("\n");
            if matches!(tc.get("hMerge"), Some("1") | Some("true")) {
                text = String::new();
            } else if matches!(tc.get("vMerge"), Some("1") | Some("true")) && r > 0 {
                text = grid[r - 1][c].clone().unwrap_or_default();
            }
            if grid[r][c].is_none() {
                grid[r][c] = Some(text);
            }
            c += 1;
        }
    }
    tables::finish(&grid)
}

// 노트 슬라이드의 본문 자리표시자 글자
fn note_texts(root: &Node) -> Vec<String> {
    let mut out = vec![];
    for sh in root.iter_tag("sp") {
        if ph_type(sh).as_deref() != Some("body") {
            continue;
        }
        out.extend(paragraphs(sh.child("txBody")).into_iter().map(|(t, _)| t));
    }
    out
}
