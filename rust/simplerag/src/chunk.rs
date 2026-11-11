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
use crate::docmodel::assemble::{build_sections, make_prefix, section_units, Section, Unit};
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
// 진단용 청크 JSON 저장 (extract --blocks --dump)
//=> blocks.json 옆에 <파일명>.chunks.json 을 쓴다. 블록이 청크로 어떻게 묶이고 잘렸는지,
//   청크마다 토큰 수 · 절 번호 · 제목 경로 · 쪽을 눈으로 확인하는 용도다(Python chunk.dump_chunks 와 같은 모양).
//   text 는 접두 "[문서 > 절]" 을 포함한 글 — 임베딩·검색·LLM 에 실제로 들어가는 글 그대로다.
// -in: chunks = split_blocks 결과 · tok = 토큰 수 세기 · out_dir = 저장 폴더(없으면 만든다)
//      name = 파일 이름 바탕(원본 파일명) · format = 추출 경로("docx"·"legacy" …)
// -out: 저장한 경로 / 쓰기 실패는 io::Error
//------------------------------------------------------------------
pub fn dump_chunks(chunks: &[ChunkMeta], tok: &Tok, out_dir: &str, name: &str, format: &str) -> std::io::Result<String> {
    std::fs::create_dir_all(out_dir)?;
    let path = pyutil::join(out_dir, &format!("{}.chunks.json", name));
    let c = cfg();
    let items: Vec<serde_json::Value> = chunks.iter().enumerate().map(|(i, m)| serde_json::json!({
        "no": i,
        "tokens": tok.tlen(&m.text),
        "section_id": m.section_id,
        "path": m.path,
        "source": m.source,
        "page": m.page,
        "page_from": m.page_from,
        "page_to": m.page_to,
        "text": m.text,
    })).collect();
    let doc = serde_json::json!({
        "file": name,
        "format": format,
        // 이 청크를 만든 설정 — 크기를 바꿔 가며 비교할 때 어느 값으로 자른 결과인지 남긴다
        "chunk_tokens": c.chunk_tokens,
        "chunk_overlap": c.chunk_overlap,
        "chunk_mode": c.chunk_mode,
        "count": items.len(),
        "chunks": items,
    });
    std::fs::write(&path, serde_json::to_string_pretty(&doc).unwrap_or_default())?;
    Ok(path)
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
    // 작은 표 통째(chunk.table_whole) — 표 글 토큰이 기준 이하면 행으로 나누지 않는다
    let whole_max = c.chunk_table_whole;
    let keep = |t: &str| tok.tlen(t) as i64 <= whole_max;
    let keep_whole: Option<&dyn Fn(&str) -> bool> = if whole_max != 0 { Some(&keep) } else { None };
    for sec in build_sections(doc) {
        let units = section_units(&sec, keep_whole);
        if units.is_empty() {
            continue;
        }
        let prefix = make_prefix(title_opt, &sec.path, 80);
        let whole = pyutil::strip(&format!("{}{}", prefix, units.iter().map(|u| u.0.as_str()).collect::<Vec<_>>().join("\n"))).to_string();
        let n = tok.tlen(&whole);
        if n <= size {
            if buf_n + n > size {
                // 모아 둔 것 끝의 '제목만 든 절'은 앞 청크에 매달지 않고 이 절과 함께 새 묶음으로 넘긴다
                let tail = peel_heading_only(&mut buf, &mut buf_secs, &sec);
                if !buf.is_empty() {
                    out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs, &mut pf));
                    buf.clear();
                    buf_secs.clear();
                }
                buf_n = 0;
                for (w, s) in tail {
                    buf_n += tok.tlen(&w);
                    buf.push(w);
                    buf_secs.push(s);
                }
            }
            buf.push(whole);
            buf_secs.push(sec);
            buf_n += n;
            continue;
        }
        // 모아 둔 것 끝의 '제목만 든 절'은 홀로 내보내지 않고 이 절 첫 조각 앞에 제목 줄로 붙인다
        let tail = peel_heading_only(&mut buf, &mut buf_secs, &sec);
        if !buf.is_empty() {
            out.push(chunk_meta(pyutil::strip(&buf.join("\n\n")).to_string(), &buf_secs, &mut pf));
            buf.clear();
            buf_secs.clear();
        }
        buf_n = 0;
        let lead_lines: Vec<String> = tail.iter().map(|(_, s)| section_units(s, None)[0].0.clone()).collect();
        let lead = if lead_lines.is_empty() { None } else { Some(lead_lines.join("\n")) };
        let budget = std::cmp::max(c.min_chunk_tokens as usize, size.saturating_sub(tok.tlen(&prefix)));
        for piece in pack_units(&units, tok, budget, overlap, has_heading(&sec), lead) {
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

// 절 제목이 단위로 들어가는 절인가 — section_units 와 같은 조건(합성 아님 · 글자 있음)
fn has_heading(sec: &Section) -> bool {
    sec.heading.as_ref().map(|h| !h.synthetic && !pyutil::strip(&h.text).is_empty()).unwrap_or(false)
}

// 제목만 든 절인가 — 제목 바로 뒤에 하위 제목이 와서 본문 단위가 하나도 없는 절
fn heading_only(sec: &Section) -> bool {
    has_heading(sec) && section_units(sec, None).len() == 1
}

// 상위 절인가 — 위 절 경로가 아래 절 경로의 앞부분이고 아래 절이 더 깊다
fn is_ancestor(upper: &Section, lower: &Section) -> bool {
    let k = upper.path.len();
    lower.path.len() > k && lower.path[..k] == upper.path[..]
}

// 모아 둔 절 끝에서 '들어오는 절(nxt)의 상위 제목뿐인 절'들을 떼어 낸다 — (whole, 절) 목록, 순서 유지
//   상위 제목인지 꼭 본다 — 번호 조항 문서는 제목 줄이 곧 내용인 절이 줄줄이 이어진다(Python peel 과 같다)
fn peel_heading_only(buf: &mut Vec<String>, secs: &mut Vec<Section>, nxt: &Section) -> Vec<(String, Section)> {
    let mut k = secs.len();
    while k > 0 && heading_only(&secs[k - 1]) && is_ancestor(&secs[k - 1], nxt) {
        k -= 1;
    }
    buf.split_off(k).into_iter().zip(secs.split_off(k)).collect()
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
//   제목 줄만 든 조각은 끊지 않는다 — 지금 조각에 절 제목(과 넘겨받은 상위 제목, 그 뒤의 표 머리글)만
//   들었으면 '머리 상태'로 보고 다음 단위가 예산을 넘겨도 함께 담는다. 다음 단위가 혼자 예산을 넘어
//   쪼개질 때는 머리 줄을 그 첫 조각 맨 앞에 붙인다(Python chunk._pack_units 와 같다).
//   head = units[0] 이 절 제목인가 · lead = 첫 조각 맨 앞에 둘 제목 줄(앞의 '제목만 든 상위 절')
//------------------------------------------------------------------
fn pack_units(units: &[Unit], tok: &Tok, budget: usize, overlap: usize, head: bool, lead: Option<String>) -> Vec<String> {
    let c = cfg();
    let mut pieces: Vec<String> = vec![];
    let mut cur: Vec<String> = vec![];
    let mut cur_n = 0usize;
    // 지금 조각이 제목(·표 머리글) 줄뿐인가
    let mut head_only = false;
    if let Some(l) = lead {
        cur_n = tok.tlen(&l);
        cur.push(l);
        head_only = true;
    }
    let soft = budget as f64 * c.chunk_overflow_ratio.max(1.0);
    // 지금 조각을 끊어 낸다
    fn emit(cur: &mut Vec<String>, pieces: &mut Vec<String>, cur_n: &mut usize, head_only: &mut bool) {
        if !cur.is_empty() {
            pieces.push(cur.join("\n"));
            cur.clear();
        }
        *cur_n = 0;
        *head_only = false;
    }
    for (i, (text, header, whole)) in units.iter().enumerate() {
        let n = tok.tlen(text);
        let hdr = header.as_ref().filter(|h| tok.tlen(h) as i64 <= c.table_header_max_tokens).cloned();
        let nxt = units.get(i + 1);
        let is_hdr_unit = nxt.map(|(_, nh, _)| nh.as_deref() == Some(text.as_str())).unwrap_or(false);
        // 표 머리글이 첫 행과 함께 들어갈 자리가 없으면 먼저 끊는다 — 단 지금 조각이 제목뿐이면 끊지 않는다
        if !cur.is_empty() && !head_only && is_hdr_unit {
            if let Some((nt, _, _)) = nxt {
                if cur_n + n + tok.tlen(nt) > budget {
                    emit(&mut cur, &mut pieces, &mut cur_n, &mut head_only);
                }
            }
        }
        if n > budget {
            // 제목 줄뿐인 조각은 따로 내지 않고 이 단위 첫 조각 앞에 붙인다
            let mut pre: Vec<String> = vec![];
            if head_only {
                pre = std::mem::take(&mut cur);
                // 끝이 이 행의 표 머리글이면 아래에서 다시 붙으므로 뺀다(두 번 들어가지 않게)
                if let (Some(h), Some(last)) = (&hdr, pre.last()) {
                    if last == h {
                        pre.pop();
                    }
                }
                cur_n = 0;
                head_only = false;
            } else {
                emit(&mut cur, &mut pieces, &mut cur_n, &mut head_only);
            }
            // 통째 표는 약한 초과(α)를 넘어도 hard_max 안이면 쪼개지 않는다
            let mut subs = if (n as f64 <= soft || *whole) && n as i64 <= c.chunk_hard_max {
                vec![match &hdr { Some(h) => format!("{}\n{}", h, text), None => text.clone() }]
            } else {
                let mut subs = split_unit(text, tok, budget, overlap);
                if let (Some(h), Some(first)) = (&hdr, subs.first_mut()) {
                    *first = format!("{}\n{}", h, first);
                }
                subs
            };
            if !pre.is_empty() {
                match subs.first_mut() {
                    Some(first) => *first = format!("{}\n{}", pre.join("\n"), first),
                    None => pieces.push(pre.join("\n")),
                }
            }
            pieces.extend(subs);
            continue;
        }
        if cur_n + n > budget && !head_only {
            emit(&mut cur, &mut pieces, &mut cur_n, &mut head_only);
        }
        let was_head_only = head_only && !cur.is_empty();
        if cur.is_empty() {
            if let Some(h) = &hdr {
                cur.push(h.clone());
            }
        }
        cur.push(text.clone());
        cur_n += n;
        // 머리 상태가 이어지는 경우: 절 제목(첫 단위)이거나, 머리 상태에 표 머리글 단위가 더해질 때
        head_only = (head && i == 0) || (was_head_only && is_hdr_unit);
    }
    emit(&mut cur, &mut pieces, &mut cur_n, &mut head_only);
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
