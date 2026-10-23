//------------------------------------------------------------------
// 하이브리드 검색 + 리랭킹 + 부모(절) 확장 (Python retrieve/hybrid.py · parent.py)
//=> dense(벡터)와 BM25 를 각각 돌려 RRF 로 합치고, 사본을 거른 뒤
//   (켜져 있으면) 크로스인코더로 다시 줄 세운다. 관련도가 너무 낮으면 근거를 내지 않는다.
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

use fancy_regex::Regex as FRegex;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::bm25::Bm25Index;
use crate::config::cfg;
use crate::embed::{Embedder, Reranker};
use crate::pyutil;
use crate::store::{pl_str, Payload, VectorStore};

//------------------------------------------------------------------
// 근거 청크 하나
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Chunk {
    pub id: i64,
    pub text: String,
    pub doc_name: String,
    pub doc_path: String,
    pub folder: String,
    pub chunk_idx: i64,
    pub section_id: Option<i64>,
    pub child_text: Option<String>,
}

// 시간 측정 묶음(키 순서는 Python dict 와 같게)
pub type Timing = Map<String, Value>;

pub fn tget(t: &Timing, k: &str) -> f64 {
    t.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0)
}

static WS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());

//------------------------------------------------------------------
// 중복 판정 키 — 파일명 접두를 뺀 본문에서 공백을 지운 글자
//------------------------------------------------------------------
pub fn dedup_key(text: &str, doc_name: &str) -> Option<String> {
    let title = if doc_name.is_empty() { "" } else { pyutil::splitext(pyutil::basename(doc_name)).0 };
    let mut t = text.to_string();
    if !title.is_empty() {
        t = t.replace(&format!("[{} > ", title), "[> ").replace(&format!("[{}]", title), "[]");
    }
    let flat = WS.replace_all(&t, "").to_string();
    if flat.is_empty() { None } else { Some(flat) }
}

//------------------------------------------------------------------
// 순서대로 고유한 것 top_k 개(모자라면 사본으로 채움)
//------------------------------------------------------------------
pub fn pick_unique(chunks: Vec<Chunk>, top_k: usize) -> Vec<Chunk> {
    let mut picked = vec![];
    let mut seen = HashSet::new();
    let mut dropped = vec![];
    for c in chunks {
        let key = dedup_key(&c.text, &c.doc_name);
        if let Some(k) = &key {
            if seen.contains(k) {
                dropped.push(c);
                continue;
            }
            seen.insert(k.clone());
        }
        picked.push(c);
        if picked.len() >= top_k {
            return picked;
        }
    }
    picked.extend(dropped);
    picked.truncate(top_k);
    picked
}

//------------------------------------------------------------------
// RRF 융합 — 점수 = Σ 1/(k + 순위), 점수 내림차순(같으면 처음 나온 순서)
//------------------------------------------------------------------
pub fn rrf(lists: &[Vec<i64>]) -> Vec<(i64, f64)> {
    let k = cfg().rrf_k;
    let mut scores: Vec<(i64, f64)> = vec![];
    let mut pos: HashMap<i64, usize> = HashMap::new();
    for lst in lists {
        for (rank, id) in lst.iter().enumerate() {
            let add = 1.0 / (k + rank as f64 + 1.0);
            match pos.get(id) {
                Some(&i) => scores[i].1 += add,
                None => {
                    pos.insert(*id, scores.len());
                    scores.push((*id, add));
                }
            }
        }
    }
    // 파이썬 sorted 는 안정 정렬 — 같은 점수는 처음 나온 순서
    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scores
}

// 관련도 문턱(리랭커 점수) — off 면 None
pub fn relevance_min() -> Option<f64> {
    let raw = std::env::var("SIMPLERAG_RELEVANCE_MIN").unwrap_or_else(|_| "-6".into()).trim().to_lowercase();
    if raw.is_empty() || raw == "off" || raw == "none" {
        return None;
    }
    Some(raw.parse().unwrap_or(-6.0))
}
pub fn relevance_soft() -> f64 {
    std::env::var("SIMPLERAG_RELEVANCE_SOFT").ok().and_then(|v| v.trim().parse().ok()).unwrap_or(1.0)
}

static WORD: Lazy<Regex> = Lazy::new(|| Regex::new(r"[가-힣A-Za-z0-9]+").unwrap());
static JOSA: Lazy<Vec<&'static str>> = Lazy::new(|| {
    let mut v = vec!["은", "는", "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과", "도", "만",
        "요", "까", "인가요", "인가", "입니까", "나요", "은요", "는요", "이란", "란", "에게", "께",
        "부터", "까지", "보다", "처럼", "이나", "나", "하나요", "되나요", "있나요", "하면", "되면", "시"];
    // 긴 것부터(같은 길이는 원래 순서 — 파이썬 sorted 는 안정)
    v.sort_by_key(|s| std::cmp::Reverse(pyutil::clen(s)));
    v
});
const GENERIC: &[&str] = &["얼마", "무엇", "어떻게", "어떤", "언제", "누가", "어디", "알려줘", "알려", "주세요", "방법", "경우",
    "대한", "관련", "무엇인가", "뭔가", "있는", "하는", "되는", "기준", "내용", "얼마인가"];

//------------------------------------------------------------------
// 질문의 핵심 낱말 — 끝 조사를 떼고, 두 글자 이상이며 흔한 질문 말이 아닌 것
//------------------------------------------------------------------
pub fn keywords(q: &str) -> Vec<String> {
    let mut out = vec![];
    for m in WORD.find_iter(q) {
        let mut w = m.as_str().to_string();
        for j in JOSA.iter() {
            if pyutil::clen(&w) > pyutil::clen(j) + 1 && w.ends_with(j) {
                w.truncate(w.len() - j.len());
                break;
            }
        }
        if pyutil::clen(&w) >= 2 && !GENERIC.contains(&w.as_str()) {
            out.push(w);
        }
    }
    out
}

//------------------------------------------------------------------
// 핵심 낱말이 후보에 든 비율(0~1) — 세 글자 이상은 앞 두 글자만 있어도 든 것
//------------------------------------------------------------------
pub fn keyword_overlap(q: &str, texts: &[String]) -> Option<f64> {
    let kws = keywords(q);
    if kws.is_empty() {
        return None;
    }
    let blob = texts.join(" ");
    let hit = kws.iter().filter(|k| blob.contains(k.as_str()) || (pyutil::clen(k) >= 3 && blob.contains(pyutil::head(k, 2)))).count();
    Some(hit as f64 / kws.len() as f64)
}

//------------------------------------------------------------------
// 하이브리드 검색기
//------------------------------------------------------------------
pub struct Retriever {
    pub embedder: Arc<Embedder>,
    pub store: Arc<VectorStore>,
    pub bm25: Arc<Bm25Index>,
    pub reranker: std::sync::Mutex<Option<Arc<Reranker>>>,
}

fn ms(d: std::time::Duration) -> f64 {
    pyutil::round_to(d.as_secs_f64() * 1000.0, 1)
}

impl Retriever {
    //------------------------------------------------------------------
    // 검색 (핵심) — 1단계 RRF + (켜져 있으면) 리랭킹 + 관련도 판정
    //------------------------------------------------------------------
    pub fn search(&self, query: &str, top_k: Option<usize>, folder: Option<&str>) -> Result<(Vec<Chunk>, Timing), String> {
        let top_k = top_k.filter(|k| *k > 0).unwrap_or(cfg().top_k as usize);
        let rr = self.reranker.lock().unwrap().clone();
        let rr = match rr {
            None => return self.search_rrf(query, top_k, folder),
            Some(r) => r,
        };
        let pool = top_k.max(cfg().rerank_pool as usize);
        let (chunks, mut timing) = self.search_rrf(query, pool, folder)?;
        let gate = relevance_min();
        if chunks.is_empty() || (chunks.len() <= top_k && gate.is_none()) {
            return Ok((chunks, timing));
        }
        let t0 = Instant::now();
        let texts: Vec<String> = chunks.iter().map(|c| c.text.clone()).collect();
        let scores = match rr.score(query, &texts) {
            Ok(s) => s,
            Err(e) => {
                timing.insert("rerank_error".into(), json!(format!("RerankError: {}", pyutil::head(&e, 120))));
                let mut c = chunks;
                c.truncate(top_k);
                return Ok((c, timing));
            }
        };
        let rms = ms(t0.elapsed());
        let mut order: Vec<usize> = (0..chunks.len()).collect();
        order.sort_by(|a, b| scores[*b].partial_cmp(&scores[*a]).unwrap_or(std::cmp::Ordering::Equal));
        timing.insert("rerank_ms".into(), pyutil::jf(rms));
        let total = pyutil::round_to(tget(&timing, "total_ms") + rms, 1);
        timing.insert("total_ms".into(), pyutil::jf(total));
        let top = scores[order[0]] as f64;
        timing.insert("rerank_top".into(), pyutil::jf(pyutil::round_to(top, 2)));
        if let Some(g) = gate {
            let ov = keyword_overlap(query, &texts);
            timing.insert("keyword_overlap".into(), ov.map(|o| pyutil::jf(pyutil::round_to(o, 2))).unwrap_or(Value::Null));
            if top < g || (top < relevance_soft() && ov == Some(0.0)) {
                timing.insert("no_relevant".into(), json!(true));
                return Ok((vec![], timing));
            }
        }
        let ranked: Vec<Chunk> = order.into_iter().map(|i| chunks[i].clone()).collect();
        let picked = if cfg().dedup_evidence { pick_unique(ranked, top_k) } else { ranked.into_iter().take(top_k).collect() };
        Ok((picked, timing))
    }

    //------------------------------------------------------------------
    // 1단계 — 질의 임베딩 → dense·BM25 후보 → RRF → 사본 거르기 → top_k
    //------------------------------------------------------------------
    pub fn search_rrf(&self, query: &str, top_k: usize, folder: Option<&str>) -> Result<(Vec<Chunk>, Timing), String> {
        let dense_k = cfg().dense_top_k as usize;
        let bm25_k = cfg().bm25_top_k as usize;
        let t0 = Instant::now();
        let scope = match folder {
            Some(f) => Some(self.store.folder_scope(f)?),
            None => None,
        };
        if let Some((n_docs, _)) = &scope {
            if *n_docs == 0 {
                let mut t = Timing::new();
                for k in ["embed_ms", "dense_ms", "bm25_ms", "fuse_ms"] {
                    t.insert(k.into(), json!(0.0));
                }
                t.insert("total_ms".into(), pyutil::jf(ms(t0.elapsed())));
                t.insert("scope_docs".into(), json!(0));
                t.insert("scope_via".into(), json!("matrix"));
                return Ok((vec![], t));
            }
        }
        let qv = self.embedder.embed_query(query)?;
        let t1 = Instant::now();
        let allow = scope.as_ref().map(|s| &s.1);
        let dense = self.store.search(&qv, dense_k, allow)?;
        let dense_ids: Vec<i64> = dense.iter().map(|d| d.0).collect();
        let mut payloads: HashMap<i64, Payload> = dense.into_iter().map(|(i, _, p)| (i, p)).collect();
        let t2 = Instant::now();
        let tok = self.embedder.tokenizer()?;
        let bm_ids = if bm25_k > 0 { self.bm25.search(query, &tok, bm25_k, allow) } else { vec![] };
        let t3 = Instant::now();
        let mut lists = vec![dense_ids];
        if !bm_ids.is_empty() {
            lists.push(bm_ids);
        }
        let fused: Vec<i64> = rrf(&lists).into_iter().map(|(i, _)| i).collect();
        let missing: Vec<i64> = fused.iter().copied().filter(|i| !payloads.contains_key(i)).collect();
        if !missing.is_empty() {
            payloads.extend(self.store.payloads(&missing)?);
        }
        // payload 가 없는 id(이미 지운 점을 BM25 가 돌려준 것)는 버린다
        let fused: Vec<i64> = fused.into_iter().filter(|i| payloads.get(i).map(|p| !p.is_empty()).unwrap_or(false)).collect();
        let picked: Vec<i64> = if cfg().dedup_evidence { dedup(&fused, &payloads, top_k) } else { fused.into_iter().take(top_k).collect() };
        let chunks: Vec<Chunk> = picked.iter().map(|pid| {
            let pl = payloads.get(pid).cloned().unwrap_or_default();
            Chunk {
                id: *pid,
                text: pl_str(&pl, "text").to_string(),
                doc_name: pl_str(&pl, "doc_name").to_string(),
                doc_path: pl_str(&pl, "doc_path").to_string(),
                folder: pl_str(&pl, "folder").to_string(),
                chunk_idx: pl.get("chunk_idx").and_then(|v| v.as_i64()).unwrap_or(0),
                section_id: pl.get("section_id").and_then(|v| v.as_i64()),
                child_text: None,
            }
        }).collect();
        let t4 = Instant::now();
        let mut timing = Timing::new();
        timing.insert("embed_ms".into(), pyutil::jf(ms(t1 - t0)));
        timing.insert("dense_ms".into(), pyutil::jf(ms(t2 - t1)));
        timing.insert("bm25_ms".into(), pyutil::jf(ms(t3 - t2)));
        timing.insert("fuse_ms".into(), pyutil::jf(ms(t4 - t3)));
        timing.insert("total_ms".into(), pyutil::jf(ms(t4 - t0)));
        if let Some((n, _)) = scope {
            timing.insert("scope_docs".into(), json!(n));
            timing.insert("scope_via".into(), json!("matrix"));
        }
        Ok((chunks, timing))
    }
}

// 융합 순위에서 사본 거르기(모자라면 사본으로 채움)
fn dedup(order: &[i64], payloads: &HashMap<i64, Payload>, top_k: usize) -> Vec<i64> {
    let mut picked = vec![];
    let mut seen = HashSet::new();
    let mut dropped = vec![];
    for pid in order {
        let pl = &payloads[pid];
        let key = dedup_key(pl_str(pl, "text"), pl_str(pl, "doc_name"));
        if let Some(k) = &key {
            if seen.contains(k) {
                dropped.push(*pid);
                continue;
            }
            seen.insert(k.clone());
        }
        picked.push(*pid);
        if picked.len() >= top_k {
            return picked;
        }
    }
    picked.extend(dropped);
    picked.truncate(top_k);
    picked
}

// ── Parent-Child (부모 절) ───────────────────────────────

static PREFIX_LINE: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"^\[[^\n]*\]$").unwrap());

// 청크 글 → (접두 줄, 본문)
fn split_prefix(text: &str) -> (String, String) {
    let (first, _, rest) = pyutil::partition(text, "\n");
    if PREFIX_LINE.is_match(pyutil::strip(first)).unwrap_or(false) {
        (pyutil::strip(first).to_string(), pyutil::strip(rest).to_string())
    } else {
        (String::new(), pyutil::strip(text).to_string())
    }
}

//------------------------------------------------------------------
// 부모 글 만들기 — 자식을 가운데 두고 뒤·앞 형제를 번갈아 붙이다 상한에서 멈춘다
//------------------------------------------------------------------
pub fn build_parent(child: &Chunk, siblings: &[Payload], tlen: &dyn Fn(&str) -> usize, max_tokens: usize) -> String {
    if siblings.len() <= 1 {
        return child.text.clone();
    }
    let idx = match siblings.iter().position(|s| s.get("chunk_idx").and_then(|v| v.as_i64()) == Some(child.chunk_idx)) {
        Some(i) => i,
        None => return child.text.clone(),
    };
    let (prefix, _) = split_prefix(&child.text);
    let bodies: Vec<String> = siblings.iter().map(|s| split_prefix(pl_str(s, "text")).1).collect();
    let lens: Vec<usize> = bodies.iter().map(|b| tlen(b)).collect();
    let mut chosen: HashSet<usize> = [idx].into_iter().collect();
    let mut total = tlen(&prefix) + lens[idx];
    let (mut lo, mut hi) = (idx as i64 - 1, idx + 1);
    let (mut lo_ok, mut hi_ok) = (true, true);
    while lo_ok || hi_ok {
        let mut grew = false;
        if hi_ok && hi < bodies.len() && total + lens[hi] <= max_tokens {
            chosen.insert(hi);
            total += lens[hi];
            hi += 1;
            grew = true;
        } else {
            hi_ok = false;
        }
        if lo_ok && lo >= 0 && total + lens[lo as usize] <= max_tokens {
            chosen.insert(lo as usize);
            total += lens[lo as usize];
            lo -= 1;
            grew = true;
        } else {
            lo_ok = false;
        }
        if !grew {
            break;
        }
    }
    let mut keys: Vec<usize> = chosen.into_iter().collect();
    keys.sort();
    let body = keys.iter().filter(|j| !bodies[**j].is_empty()).map(|j| bodies[*j].as_str()).collect::<Vec<_>>().join("\n");
    if prefix.is_empty() { body } else { pyutil::strip(&format!("{}\n{}", prefix, body)).to_string() }
}

//------------------------------------------------------------------
// 검색 결과를 부모로 넓히기 — 이미 넣은 절은 건너뛰고 top_k 개
//------------------------------------------------------------------
pub fn expand(chunks: Vec<Chunk>, store: &VectorStore, tlen: &dyn Fn(&str) -> usize, top_k: usize, max_tokens: usize) -> Vec<Chunk> {
    let mut out = vec![];
    let mut seen: HashSet<(String, String)> = HashSet::new();
    for c in chunks {
        let key = match c.section_id {
            Some(s) => (c.doc_path.clone(), s.to_string()),
            None => ("id".to_string(), format!("#{}", c.id)),
        };
        if !seen.insert(key) {
            continue;
        }
        let mut text = c.text.clone();
        if let Some(sid) = c.section_id {
            if let Ok(sibs) = store.section_chunks(&c.doc_path, sid) {
                text = build_parent(&c, &sibs, tlen, max_tokens);
            }
        }
        let mut nc = c.clone();
        nc.child_text = Some(c.text.clone());
        nc.text = text;
        out.push(nc);
        if out.len() >= top_k {
            break;
        }
    }
    out
}
