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
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct ChunkMeta {
    pub text: String,
    pub section_id: Option<i64>,
    pub path: Vec<String>,
    pub source: String,
    pub page_from: Option<i64>,
    pub page_to: Option<i64>,
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
            text: t, section_id: None, path: vec![], source: "legacy".into(), page_from: None, page_to: None,
        }).collect();
    }
    let mut out: Vec<ChunkMeta> = vec![];
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
                    out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs));
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
            out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs));
            buf.clear();
            buf_secs.clear();
        }
        buf_n = 0;
        let budget = std::cmp::max(c.min_chunk_tokens as usize, size.saturating_sub(tok.tlen(&prefix)));
        for piece in pack_units(&units, tok, budget, overlap) {
            if !pyutil::strip(&piece).is_empty() {
                out.push(chunk_meta(pyutil::strip(&format!("{}{}", prefix, piece)).to_string(), std::slice::from_ref(&sec)));
            }
        }
    }
    if !buf.is_empty() {
        out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs));
    }
    let out = enforce_hard_max(out, tok, overlap);
    out.into_iter().filter(|m| tok.tlen(&m.text) as i64 >= c.min_chunk_tokens).collect()
}

// 청크 출처 정보 — 첫 절 기준, 쪽 범위는 모든 절의 합
fn chunk_meta(text: String, secs: &[Section]) -> ChunkMeta {
    let first = &secs[0];
    let pages: Vec<i64> = secs.iter().flat_map(|s| [s.page_from, s.page_to]).flatten().collect();
    let src = first.heading.as_ref().map(|h| h.source).unwrap_or("parser");
    ChunkMeta {
        text,
        section_id: Some(first.id),
        path: first.path.clone(),
        source: src.to_string(),
        page_from: pages.iter().min().copied(),
        page_to: pages.iter().max().copied(),
    }
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
