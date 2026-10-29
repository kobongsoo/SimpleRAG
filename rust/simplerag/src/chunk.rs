//------------------------------------------------------------------
// 청킹 (Python chunk.py)
//=> 세 가지 길이 있다.
//    · split_text       — 고정 크기(토큰 창 + 겹침)
//    · split_structured — 평문을 절(조문·번호절 …)로 나눠 절 경계를 지키며 묶는다(legacy 엔진)
//    · split_blocks     — 블록 문서를 절 단위로 묶는다(blocks 엔진). 표는 행 경계에서 자르고
//                         조각마다 머리글 행을 다시 붙인다
//   모두 e5 토크나이저 기준 토큰 수로 잰다.
//------------------------------------------------------------------

use fancy_regex::Regex as FRegex;
use once_cell::sync::Lazy;

use crate::config::cfg;
use crate::docmodel::assemble::{build_sections, make_prefix, section_units, Section};
use crate::docmodel::blocks::Document;
use crate::pyutil;
use crate::structure;
use crate::tok::Tok;

//------------------------------------------------------------------
// 청크 하나 + 출처 정보 (split_blocks 결과)
// -필드: text = 청크 글 / section_id·path·source·page_from·page_to = payload 에 남길 정보
//        page = 청크가 실제로 시작하는 쪽(블록 지문 대조로 찾음, 못 찾으면 None)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct ChunkMeta {
    pub text: String,
    pub section_id: Option<i64>,
    pub path: Vec<String>,
    pub source: String,
    pub page_from: Option<i64>,
    pub page_to: Option<i64>,
    pub page: Option<i64>,
}

//------------------------------------------------------------------
// 토큰 id 를 창으로 자르기 — 꼬리가 MIN_CHUNK_TOKENS 보다 짧으면 버린다
//------------------------------------------------------------------
pub fn chunk_ids(ids: &[u32], size: usize, overlap: usize) -> Vec<Vec<u32>> {
    if ids.is_empty() {
        return vec![];
    }
    let step = std::cmp::max(1, size as i64 - std::cmp::min(overlap as i64, size as i64 - 1)) as usize;
    let min = cfg().min_chunk_tokens as usize;
    let mut out = vec![];
    let mut start = 0;
    while start < ids.len() {
        let piece = &ids[start..(start + size).min(ids.len())];
        if piece.len() >= min {
            out.push(piece.to_vec());
        }
        if start + size >= ids.len() {
            break;
        }
        start += step;
    }
    out
}

//------------------------------------------------------------------
// 고정 크기 청킹
//------------------------------------------------------------------
pub fn split_text(text: &str, tok: &Tok, size: Option<usize>, overlap: Option<usize>) -> Vec<String> {
    if pyutil::strip(text).is_empty() {
        return vec![];
    }
    let size = size.unwrap_or(cfg().chunk_tokens as usize);
    let overlap = overlap.unwrap_or(cfg().chunk_overlap as usize);
    let ids = tok.ids(text);
    chunk_ids(&ids, size, overlap).iter().map(|p| tok.decode(p)).collect()
}

//------------------------------------------------------------------
// 예산을 넘는 본문을 줄 단위로 나누기(한 줄이 넘치면 토큰 창으로)
//------------------------------------------------------------------
fn split_oversized(body: &str, tok: &Tok, budget: usize, overlap: usize) -> Vec<String> {
    let units: Vec<&str> = body.split('\n').filter(|u| !pyutil::strip(u).is_empty()).collect();
    let mut out = vec![];
    let mut cur: Vec<&str> = vec![];
    let mut cur_n = 0;
    for u in units {
        let n = tok.tlen(u);
        if n > budget {
            if !cur.is_empty() {
                out.push(cur.join("\n"));
                cur.clear();
                cur_n = 0;
            }
            let ids = tok.ids(u);
            out.extend(chunk_ids(&ids, budget, overlap).iter().map(|p| tok.decode(p)));
            continue;
        }
        if cur_n + n > budget {
            out.push(cur.join("\n"));
            cur.clear();
            cur_n = 0;
        }
        cur.push(u);
        cur_n += n;
    }
    if !cur.is_empty() {
        out.push(cur.join("\n"));
    }
    out
}

//------------------------------------------------------------------
// 구조 청킹 (legacy 엔진·폴백 문서)
//=> 작은 절은 예산 안에서 이어 묶고, 큰 절은 줄 단위로 나눈 뒤 조각마다 접두를 붙인다.
//   표 조각이 머리글 없이 시작하면 그 표의 머리글 줄을 다시 붙인다(상한 이하일 때만).
//------------------------------------------------------------------
pub fn split_structured(text: &str, tok: &Tok, title: Option<&str>, size: Option<usize>, overlap: Option<usize>) -> Vec<String> {
    if pyutil::strip(text).is_empty() {
        return vec![];
    }
    let c = cfg();
    let size = size.unwrap_or(c.chunk_tokens as usize);
    let overlap = overlap.unwrap_or(c.chunk_overlap as usize);
    let mut out: Vec<String> = vec![];
    let mut buf: Vec<String> = vec![];
    let mut buf_n = 0;
    let flush = |buf: &mut Vec<String>, out: &mut Vec<String>| {
        if !buf.is_empty() {
            out.push(pyutil::strip(&buf.join("\n\n")).to_string());
            buf.clear();
        }
    };
    for (section, body) in structure::split_sections(text) {
        let prefix = if title.map(|t| !t.is_empty()).unwrap_or(false) || section.is_some() {
            structure::make_prefix(title, section.as_deref())
        } else {
            String::new()
        };
        let lines: Vec<&str> = body.split('\n').collect();
        let heads = structure::table_headers(&lines);
        let whole = pyutil::strip(&format!("{}{}", prefix, body)).to_string();
        let n = tok.tlen(&whole);
        if n <= size {
            if buf_n + n > size {
                flush(&mut buf, &mut out);
                buf_n = 0;
            }
            buf.push(whole);
            buf_n += n;
            continue;
        }
        flush(&mut buf, &mut out);
        buf_n = 0;
        let budget = std::cmp::max(c.min_chunk_tokens as usize, size.saturating_sub(tok.tlen(&prefix)));
        for piece in split_oversized(&body, tok, budget, overlap) {
            if pyutil::strip(&piece).is_empty() {
                continue;
            }
            let mut piece = piece;
            let first = pyutil::lstrip(&piece).split('\n').next().unwrap_or("").to_string();
            if !heads.is_empty() && first.starts_with('|') {
                let hdr = heads.iter().find(|(i, _)| pyutil::strip(lines[**i]) == pyutil::strip(&first)).map(|(_, h)| h.clone());
                if let Some(h) = hdr {
                    if !piece.contains(&h) && tok.tlen(&h) as i64 <= c.table_header_max_tokens {
                        piece = format!("{}\n{}", h, piece);
                    }
                }
            }
            out.push(pyutil::strip(&format!("{}{}", prefix, piece)).to_string());
        }
    }
    flush(&mut buf, &mut out);
    out.into_iter().filter(|c2| tok.tlen(c2) as i64 >= c.min_chunk_tokens).collect()
}

static SENT_END: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"(?<=[.!?。])\s+").unwrap());

//------------------------------------------------------------------
// 블록 문서 청킹 (blocks 엔진)
//=> 폴백 평문 문서는 split_structured 그대로(legacy 와 같은 청크).
//------------------------------------------------------------------
pub fn split_blocks(doc: &mut Document, tok: &Tok, title: Option<&str>) -> Vec<ChunkMeta> {
    let c = cfg();
    let title: String = title.map(|t| t.to_string()).unwrap_or_else(|| doc.title.clone());
    let size = c.chunk_tokens as usize;
    let overlap = c.chunk_overlap as usize;
    if !doc.is_blocks() {
        let raw = doc.raw_text.clone().unwrap_or_default();
        return split_structured(&raw, tok, Some(&title), Some(size), Some(overlap)).into_iter().map(|t| ChunkMeta {
            text: t, section_id: None, path: vec![], source: "legacy".into(), page_from: None, page_to: None, page: None,
        }).collect();
    }
    let mut out: Vec<ChunkMeta> = vec![];
    let mut pf = PageFinder::default();
    let mut buf: Vec<String> = vec![];
    let mut buf_n = 0;
    let mut buf_secs: Vec<Section> = vec![];
    let title_opt = if title.is_empty() { None } else { Some(title.as_str()) };
    for sec in build_sections(doc) {
        let units = section_units(&sec);
        if units.is_empty() {
            continue;
        }
        let prefix = make_prefix(title_opt, &sec.path, 80);
        let whole = pyutil::strip(&format!("{}{}", prefix, units.iter().map(|u| u.0.as_str()).collect::<Vec<_>>().join("\n"))).to_string();
        let n = tok.tlen(&whole);
        if n <= size {
            if buf_n + n > size {
                if !buf.is_empty() {
                    out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs, &mut pf));
                    buf.clear();
                    buf_secs.clear();
                }
                buf_n = 0;
            }
            buf.push(whole);
            buf_secs.push(sec);
            buf_n += n;
            continue;
        }
        if !buf.is_empty() {
            out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs, &mut pf));
            buf.clear();
            buf_secs.clear();
        }
        buf_n = 0;
        let budget = std::cmp::max(c.min_chunk_tokens as usize, size.saturating_sub(tok.tlen(&prefix)));
        for piece in pack_units(&units, tok, budget, overlap) {
            if !pyutil::strip(&piece).is_empty() {
                out.push(chunk_meta(pyutil::strip(&format!("{}{}", prefix, piece)).to_string(), std::slice::from_ref(&sec), &mut pf));
            }
        }
    }
    if !buf.is_empty() {
        out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs, &mut pf));
    }
    let out = enforce_hard_max(out, tok, overlap);
    out.into_iter().filter(|m| tok.tlen(&m.text) as i64 >= c.min_chunk_tokens).collect()
}

// 청크 출처 정보 — 첫 절 기준. 쪽은 청크가 절 글의 어디에 있는지로 찾는다(탐색기 모드 설계서 §13)
fn chunk_meta(text: String, secs: &[Section], pf: &mut PageFinder) -> ChunkMeta {
    let first = &secs[0];
    let pages: Vec<i64> = secs.iter().flat_map(|s| [s.page_from, s.page_to]).flatten().collect();
    let src = first.heading.as_ref().map(|h| h.source).unwrap_or("parser");
    let (page, from, to) = match pf.locate(&text, secs) {
        // 청크 자리를 찾았으면 쪽 범위를 그 자리로 좁힌다
        Some((p, f, t)) => (Some(p), Some(f), Some(t)),
        // 못 찾으면 예전처럼 절 범위(넓지만 틀리지는 않음)
        None => (None, pages.iter().min().copied(), pages.iter().max().copied()),
    };
    ChunkMeta {
        text,
        section_id: Some(first.id),
        path: first.path.clone(),
        source: src.to_string(),
        page_from: from,
        page_to: to,
        page,
    }
}

const FIND_LEN: usize = 24;                 // 청크 머리·꼬리에서 대조할 글자 수(공백 뺀)
const FIND_TRY: [usize; 4] = [0, 40, 80, 120]; // 머리 대조가 안 되면 이만큼 뒤에서 다시(표 머리글을 다시 붙인 조각)
const FIND_MIN: usize = 8;                  // 이보다 짧으면 흔한 글이라 대조하지 않는다

//------------------------------------------------------------------
// 청크의 실제 쪽 찾기 (절 글 대조)
//=> 청킹은 절의 단위(제목·블록·표 행)를 이어 붙여 자르므로, 청크는 절 글의 한 토막이다.
//   그래서 절 글(공백 뺀)과 글자마다의 쪽을 만들어 두고, 청크 머리·꼬리가 절 글 어디에 있는지 찾는다.
//    1) 청크 앞의 접두 "[문서 > 절]\n" 은 뗀다(절 글에는 없다)
//    2) 머리 24자를 첫 절 글에서 찾는다 — 같은 절의 앞 청크 자리 뒤부터(같은 글귀가 반복돼도 순서대로).
//       표 머리글을 다시 붙인 조각은 머리가 앞쪽 표 머리에 걸리므로 40·80·120자 뒤에서도 찾아 본다
//    3) 꼬리 24자를 마지막 절 글에서 찾는다(머리 자리 뒤부터) → 끝 쪽
//   Python chunk.py _PageFinder 와 같은 규칙.
// -필드: secs = 절 번호 → (절 글, 바이트마다의 쪽, 다음 찾기 시작 자리)
//------------------------------------------------------------------
#[derive(Default)]
pub struct PageFinder {
    secs: std::collections::HashMap<i64, (String, Vec<Option<i64>>, usize)>,
}

impl PageFinder {
    // 절 글 준비(한 번 만들어 둔다)
    fn sec(&mut self, sec: &Section) -> &mut (String, Vec<Option<i64>>, usize) {
        self.secs.entry(sec.id).or_insert_with(|| {
            let mut hay = String::new();
            let mut pg = vec![];
            for (t, p) in unit_pages(sec) {
                for ch in t.chars().filter(|c| !pyutil::is_space(*c)) {
                    hay.push(ch);
                    pg.extend(std::iter::repeat(p).take(ch.len_utf8()));
                }
            }
            (hay, pg, 0)
        })
    }

    // 청크 → Some((시작 쪽, 최소 쪽, 최대 쪽)) / 못 찾으면 None
    pub fn locate(&mut self, text: &str, secs: &[Section]) -> Option<(i64, i64, i64)> {
        // 접두는 "[문서 > 절]\n" 모양이다(make_prefix)
        let body = match (text.starts_with('['), text.find("]\n")) {
            (true, Some(i)) => &text[i + 2..],
            _ => text,
        };
        let sq: Vec<char> = body.chars().filter(|c| !pyutil::is_space(*c)).collect();
        let (start_pos, p0) = {
            let (hay, pg, cur) = self.sec(&secs[0]);
            let mut hit: Option<usize> = None;
            for off in FIND_TRY {
                let needle: String = sq.iter().skip(off).take(FIND_LEN).collect();
                if needle.chars().count() < FIND_MIN {
                    break;
                }
                // 앞 청크 자리 뒤에서 먼저, 없으면 절 처음부터
                hit = hay[*cur..].find(&needle).map(|i| i + *cur).or_else(|| hay.find(&needle));
                if hit.is_some() {
                    break;
                }
            }
            let pos = hit?;
            let p0 = pg[pos]?;
            // 다음 청크는 이 자리 뒤부터(겹침이 있어 끝이 아니라 시작 뒤로 둔다)
            *cur = pos + hay[pos..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
            (pos, p0)
        };
        // 꼬리 — 마지막 절 글에서
        let tail: String = sq[sq.len().saturating_sub(FIND_LEN)..].iter().collect();
        let same = secs.len() == 1;
        let (hay, pg, _) = self.sec(&secs[secs.len() - 1]);
        let from = if same { start_pos } else { 0 };
        let p1 = if tail.chars().count() >= FIND_MIN {
            hay[from..].find(&tail).map(|i| i + from + tail.len() - 1).and_then(|e| pg.get(e).copied().flatten())
        } else {
            None
        };
        let p1 = p1.unwrap_or(p0);
        Some((p0, p0.min(p1), p0.max(p1)))
    }
}

//------------------------------------------------------------------
// 절 단위와 그 쪽 (section_units 와 같은 순서·같은 글)
//=> 제목은 제목 블록의 쪽, 표 머리글·행은 표 블록의 쪽, 그 밖은 블록의 쪽.
//------------------------------------------------------------------
fn unit_pages(sec: &Section) -> Vec<(String, Option<i64>)> {
    let mut out = vec![];
    if let Some(h) = &sec.heading {
        if !h.synthetic && !pyutil::strip(&h.text).is_empty() {
            out.push((h.text.clone(), h.page));
        }
    }
    for b in &sec.blocks {
        // 표든 문단이든 공백을 빼고 대조하므로 블록 글 전체를 그 블록의 쪽으로 둔다
        if !pyutil::strip(&b.text).is_empty() {
            out.push((b.text.clone(), b.page));
        }
    }
    out
}

//------------------------------------------------------------------
// 절 단위들을 예산 안에서 묶기
//=> 표 머리글 단위가 조각 끝에 홀로 남지 않게 미리 끊고, 값 행 조각이 새로 시작하면 머리글을 붙인다.
//   예산을 넘는 단위는 약한 초과(α) 안이면 통째로, 아니면 줄·토큰으로 나눈다.
//------------------------------------------------------------------
fn pack_units(units: &[(String, Option<String>)], tok: &Tok, budget: usize, overlap: usize) -> Vec<String> {
    let c = cfg();
    let mut pieces: Vec<String> = vec![];
    let mut cur: Vec<String> = vec![];
    let mut cur_n = 0usize;
    let soft = budget as f64 * c.chunk_overflow_ratio.max(1.0);
    let emit = |cur: &mut Vec<String>, pieces: &mut Vec<String>| {
        if !cur.is_empty() {
            pieces.push(cur.join("\n"));
            cur.clear();
        }
    };
    for (i, (text, header)) in units.iter().enumerate() {
        let n = tok.tlen(text);
        let hdr = header.as_ref().filter(|h| tok.tlen(h) as i64 <= c.table_header_max_tokens).cloned();
        let nxt = units.get(i + 1);
        if !cur.is_empty() {
            if let Some((nt, nh)) = nxt {
                if nh.as_deref() == Some(text.as_str()) && cur_n + n + tok.tlen(nt) > budget {
                    emit(&mut cur, &mut pieces);
                    cur_n = 0;
                }
            }
        }
        if n > budget {
            emit(&mut cur, &mut pieces);
            cur_n = 0;
            if n as f64 <= soft && n as i64 <= c.chunk_hard_max {
                pieces.push(match &hdr { Some(h) => format!("{}\n{}", h, text), None => text.clone() });
            } else {
                let mut subs = split_unit(text, tok, budget, overlap);
                if let (Some(h), Some(first)) = (&hdr, subs.first_mut()) {
                    *first = format!("{}\n{}", h, first);
                }
                pieces.extend(subs);
            }
            continue;
        }
        if cur_n + n > budget {
            emit(&mut cur, &mut pieces);
            cur_n = 0;
        }
        if cur.is_empty() {
            if let Some(h) = &hdr {
                cur.push(h.clone());
            }
        }
        cur.push(text.clone());
        cur_n += n;
    }
    emit(&mut cur, &mut pieces);
    pieces
}

// 큰 단위 하나 나누기 — 설정이 켜져 있으면 넘치는 줄을 문장 경계로도 나눈다
fn split_unit(text: &str, tok: &Tok, budget: usize, overlap: usize) -> Vec<String> {
    if !cfg().chunk_sentence_split {
        return split_oversized(text, tok, budget, overlap);
    }
    let mut lines: Vec<String> = vec![];
    for ln in text.split('\n') {
        if !pyutil::strip(ln).is_empty() && tok.tlen(ln) > budget {
            let mut last = 0;
            for m in SENT_END.find_iter(ln).filter_map(|m| m.ok()) {
                let s = &ln[last..m.start()];
                if !pyutil::strip(s).is_empty() {
                    lines.push(s.to_string());
                }
                last = m.end();
            }
            let s = &ln[last..];
            if !pyutil::strip(s).is_empty() {
                lines.push(s.to_string());
            }
        } else {
            lines.push(ln.to_string());
        }
    }
    split_oversized(&lines.join("\n"), tok, budget, overlap)
}

// 절대 상한(e5 입력 한계)을 넘는 청크는 토큰 창으로 다시 자른다
fn enforce_hard_max(chunks: Vec<ChunkMeta>, tok: &Tok, overlap: usize) -> Vec<ChunkMeta> {
    let hard = cfg().chunk_hard_max as usize;
    let mut out = vec![];
    for c in chunks {
        if tok.tlen(&c.text) <= hard {
            out.push(c);
            continue;
        }
        let ids = tok.ids(&c.text);
        for piece in chunk_ids(&ids, hard, overlap) {
            let mut m = c.clone();
            m.text = tok.decode(&piece);
            out.push(m);
        }
    }
    out
}
