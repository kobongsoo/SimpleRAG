//------------------------------------------------------------------
// 블록 → 절 조립 (Python docmodel/assemble.py)
//=> 제목 블록의 수준으로 '제목 경로 스택'을 쌓으며 블록을 절로 묶는다.
//   자르는 일(토큰 예산)은 chunk::split_blocks 가 하고, 여기는 '어디까지가 한 절인가'만 정한다.
//------------------------------------------------------------------

use super::blocks::{self as B, Block, Document};
use crate::pyutil;
use crate::structure;

//------------------------------------------------------------------
// 절 하나
// -필드: id        = 문서 안 절 번호(0부터) — payload section_id
// -필드: path      = 제목 경로 이름표
// -필드: heading   = 절을 연 제목 블록(머리말 절이면 None)
// -필드: blocks    = 본문 블록(제목·제외 블록 빼고)
// -필드: page_from, page_to = 쪽 범위
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct Section {
    pub id: i64,
    pub path: Vec<String>,
    pub heading: Option<Block>,
    pub blocks: Vec<Block>,
    pub page_from: Option<i64>,
    pub page_to: Option<i64>,
}

//------------------------------------------------------------------
// 제목 글자 → 경로 이름표 (structure._label 과 같은 규칙)
//------------------------------------------------------------------
pub fn label_of(text: &str) -> Option<String> {
    let flat = pyutil::collapse_ws(text);
    if flat.is_empty() {
        return None;
    }
    if pyutil::clen(&flat) <= structure::TITLE_MAX {
        let l = pyutil::strip(pyutil::lstrip_chars(&flat, "#")).to_string();
        return if l.is_empty() { None } else { Some(l) };
    }
    if let Some(c) = structure::NUMBERING.captures(&flat) {
        return Some(pyutil::strip(c.get(1).unwrap().as_str()).to_string());
    }
    Some(format!("{}…", pyutil::rstrip(pyutil::head(&flat, structure::TITLE_MAX))))
}

//------------------------------------------------------------------
// 절 조립 (핵심)
//    1) 제외 블록은 건너뛴다
//    2) 첫 제목 앞 블록은 경로 없는 머리말 절로 모은다
//    3) 제목이 나오면 스택을 그 수준-1 깊이까지 줄이고 push → 새 절
//    4) 블록마다 path 를 채우고 절의 쪽 범위를 넓힌다
//
// -in: doc = 후처리를 거친 문서(블록 path 를 채우므로 &mut)
//
// -out: Vec<Section>
// -out: error = 없음
//------------------------------------------------------------------
pub fn build_sections(doc: &mut Document) -> Vec<Section> {
    let mut sections: Vec<Section> = vec![];
    let mut stack: Vec<(i64, Option<String>)> = vec![];
    let mut cur = Section::default();
    // 지금 절을 목록에 넣는다(빈 머리말 절은 버린다)
    fn close(cur: Section, sections: &mut Vec<Section>) {
        if cur.heading.is_some() || !cur.blocks.is_empty() {
            let mut c = cur;
            c.id = sections.len() as i64;
            sections.push(c);
        }
    }
    for b in doc.blocks.iter_mut() {
        if B::excluded(b.kind) {
            continue;
        }
        if b.kind == B::HEADING {
            close(std::mem::take(&mut cur), &mut sections);
            let level = b.level.max(1);
            while stack.last().map(|s| s.0 >= level).unwrap_or(false) {
                stack.pop();
            }
            let label = if b.synthetic { Some(pyutil::strip(&b.text).to_string()) } else { label_of(&b.text) };
            stack.push((level, label));
            let path: Vec<String> = stack.iter().filter_map(|(_, l)| l.clone()).filter(|l| !l.is_empty()).collect();
            b.path = path.clone();
            cur = Section { id: 0, path, heading: Some(b.clone()), blocks: vec![], page_from: b.page, page_to: b.page };
            continue;
        }
        b.path = cur.path.clone();
        cur.blocks.push(b.clone());
        if let Some(p) = b.page {
            cur.page_from = Some(cur.page_from.map_or(p, |x| x.min(p)));
            cur.page_to = Some(cur.page_to.map_or(p, |x| x.max(p)));
        }
    }
    close(cur, &mut sections);
    sections
}

//------------------------------------------------------------------
// 절 본문의 단위 목록 — (글자, 머리글)
//=> 표는 행 하나가 단위(값 행에는 머리글을 함께 적는다), 그 밖은 블록 하나가 단위.
//------------------------------------------------------------------
pub fn section_units(sec: &Section) -> Vec<(String, Option<String>)> {
    let mut units = vec![];
    if let Some(h) = &sec.heading {
        if !h.synthetic && !pyutil::strip(&h.text).is_empty() {
            units.push((pyutil::strip(&h.text).to_string(), None));
        }
    }
    for b in &sec.blocks {
        if b.kind == B::TABLE && b.rows.as_ref().map(|r| !r.is_empty()).unwrap_or(false) {
            let lines: Vec<&str> = b.text.split('\n').collect();
            let nh = b.header_rows;
            let header = if nh > 0 { Some(lines[..nh.min(lines.len())].join("\n")) } else { None };
            let header = header.filter(|h| !h.is_empty());
            if let Some(h) = &header {
                units.push((h.clone(), None));
            }
            for ln in lines.iter().skip(nh) {
                units.push((ln.to_string(), header.clone()));
            }
            continue;
        }
        if !pyutil::strip(&b.text).is_empty() {
            units.push((strip_blank_lines(&b.text), None));
        }
    }
    units
}

// 앞뒤 공백·빈 줄 걷기
fn strip_blank_lines(s: &str) -> String {
    pyutil::strip(s).split('\n').filter(|l| !pyutil::strip(l).is_empty()).map(pyutil::rstrip)
        .collect::<Vec<_>>().join("\n")
}

//------------------------------------------------------------------
// 청크 접두 "[문서 > … > 절]\n" — 길면 가운데부터 '…' 로 줄인다(처음·끝은 지킨다)
//------------------------------------------------------------------
pub fn make_prefix(title: Option<&str>, path: &[String], max_chars: usize) -> String {
    let mut parts: Vec<String> = title.into_iter().map(|s| s.to_string()).chain(path.iter().cloned())
        .filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return String::new();
    }
    while pyutil::clen(&parts.join(" > ")) > max_chars && parts.len() > 2 {
        let mut mid = parts.len() / 2;
        if parts[mid] == "…" {
            mid = if mid >= 2 { mid - 1 } else { mid + 1 };
            if mid >= parts.len() - 1 {
                break;
            }
        }
        parts[mid] = "…".to_string();
        let mut merged: Vec<String> = vec![];
        for (i, p) in parts.iter().enumerate() {
            if p == "…" && i > 0 && parts[i - 1] == "…" {
                continue;
            }
            merged.push(p.clone());
        }
        parts = merged;
    }
    let joined = parts.join(" > ");
    let collapsed: String = {
        // re.sub(r"\s+", " ", …)
        let mut out = String::new();
        let mut in_ws = false;
        for ch in joined.chars() {
            if pyutil::is_space(ch) {
                if !in_ws {
                    out.push(' ');
                }
                in_ws = true;
            } else {
                out.push(ch);
                in_ws = false;
            }
        }
        out
    };
    format!("[{}]\n", collapsed)
}
