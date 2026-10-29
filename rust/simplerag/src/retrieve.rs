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
// 동의어 넓힘 때 덧붙일 근거 (Python hybrid.synonym_extra 와 같다)
//=> 동의어 사전이 질문을 넓혀 RRF 1위로 끌어올린 청크를, 리랭커가 원래 일상어 질문만 보고
//   상위 top_k 밖으로 밀어낼 때가 많다. 그때 그 청크를 top_k+1 번째 근거로 되살린다.
//    1) 설정이 꺼졌거나 이 질문에서 넓힘이 없었으면(timing 에 "synonyms" 없음) 붙이지 않는다
//    2) 근거가 top_k 를 못 채웠으면 붙이지 않는다 · 3) 이미 근거에 있거나 사본이면 붙이지 않는다
//   대가: 근거 한 건(평균 약 100토큰)만큼 TTFT 가 늘어난다(넓힘이 일어난 질문만).
//   돌려줌: 덧붙일 청크, 없으면 None
//------------------------------------------------------------------
pub fn synonym_extra(picked: &[Chunk], first: &Chunk, timing: &Timing, top_k: usize) -> Option<Chunk> {
    if !cfg().synonym_extra || !timing.contains_key("synonyms") || picked.len() < top_k {
        return None;
    }
    // 같은 청크이거나 내용이 같은 사본이면 한 건 더 넣어도 새 정보가 없다
    let key = dedup_key(&first.text, &first.doc_name);
    let dup = picked.iter().any(|c| c.id == first.id || (key.is_some() && dedup_key(&c.text, &c.doc_name) == key));
    if dup { None } else { Some(first.clone()) }
}

// 청크 글 앞의 "[문서 > 절]" 머리 한 줄 — 이웃을 이어 붙일 때 머리가 겹쳐 찍히지 않게 뗀다
static HEAD: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\[[^\]\n]*\]\n").unwrap());

//------------------------------------------------------------------
// 정밀 모드 — 근거에 이웃 청크 이어 붙이기 (Python hybrid.attach_neighbors 와 같다)
//=> 근거마다 같은 문서의 바로 앞·뒤 청크(±1) 가운데 리랭커 후보(pool) 안에 든 것만 붙인다
//   — 답이 다음 청크로 잘린 근거를 채운다(코드수정 설계서 §7 ③ C4: 정답 발췌 63 → 73).
//    1) 설정(generation.precision)이 꺼졌으면 그대로 · 2) 이미 근거로 든 청크는 붙이지 않는다
//    3) 앞 이웃은 머리 줄 뒤·본문 앞에, 뒤 이웃은 본문 뒤에 머리 줄을 떼고 잇는다
//   돌려줌: (새 근거 목록, 붙인 이웃 수)
//------------------------------------------------------------------
pub fn attach_neighbors(picked: Vec<Chunk>, pool: &[Chunk]) -> (Vec<Chunk>, usize) {
    if !cfg().precision {
        return (picked, 0);
    }
    let by: HashMap<(&str, i64), &Chunk> = pool.iter().map(|c| ((c.doc_path.as_str(), c.chunk_idx), c)).collect();
    let mut n = 0;
    let mut out = Vec::with_capacity(picked.len());
    for c in &picked {
        if c.doc_path.is_empty() {
            out.push(c.clone());
            continue;
        }
        // 같은 문서에서 이미 근거로 든 청크는 다시 넣으면 같은 글이 두 번 들어간다
        let used: HashSet<i64> = picked.iter().filter(|x| x.doc_path == c.doc_path).map(|x| x.chunk_idx).collect();
        let (mut before, mut after) = (vec![], vec![]);
        for j in [c.chunk_idx - 1, c.chunk_idx + 1] {
            let Some(p) = by.get(&(c.doc_path.as_str(), j)) else { continue };
            if used.contains(&j) {
                continue;
            }
            let body = HEAD.replacen(&p.text, 1, "").into_owned();
            if j < c.chunk_idx { before.push(body) } else { after.push(body) }
        }
        if before.is_empty() && after.is_empty() {
            out.push(c.clone());
            continue;
        }
        let mut text = c.text.clone();
        if !before.is_empty() {
            let head = HEAD.find(&text).map(|m| m.as_str().to_string()).unwrap_or_default();
            text = format!("{}{}\n{}", head, before.join("\n"), HEAD.replacen(&text, 1, ""));
        }
        if !after.is_empty() {
            text = format!("{}\n{}", text, after.join("\n"));
        }
        n += before.len() + after.len();
        out.push(Chunk { text, ..c.clone() });
    }
    (out, n)
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
        self.search_with(query, top_k, folder, None)
    }

    //------------------------------------------------------------------
    // 검색 — corpus 를 주면 인덱싱 폴더 인덱스는 보지 않고 그 대화에 올린 문서에서만 찾는다
    //   (Python HybridRetriever.search 의 corpus 와 같다 · 문서 업로드 설계서 §6)
    //------------------------------------------------------------------
    pub fn search_with(&self, query: &str, top_k: Option<usize>, folder: Option<&str>,
                       corpus: Option<&crate::uploads::UploadCorpus>) -> Result<(Vec<Chunk>, Timing), String> {
        let top_k = top_k.filter(|k| *k > 0).unwrap_or(cfg().top_k as usize);
        // 업로드 문서만 — 폴더 한정은 의미가 없다
        let folder = if corpus.is_some() { None } else { folder };
        // 메타데이터 조건(사람 · 기간 · 형식 · 출처 · 스캔) — 조건이 없는 질의는 mp 가 None 이라 예전과 똑같다
        let mp = if corpus.is_none() { meta_plan(query, folder) } else { None };
        if let Some(p) = &mp {
            if p.meta_only {
                // 본문 검색어가 남지 않았다 — 조건에 맞는 문서를 가산 · 수정일 순으로(§13-2)
                return self.meta_only(p, top_k);
            }
        }
        let query: &str = mp.as_ref().map(|p| p.query.as_str()).unwrap_or(query);
        let docs = mp.as_ref().and_then(|p| p.docs.as_ref());
        let rr = self.reranker.lock().unwrap().clone();
        let rr = match rr {
            None => {
                let (c, t) = self.search_rrf_docs(query, top_k, folder, corpus, 0, docs)?;
                return Ok((c, meta_timing(t, mp.as_ref())));
            }
            Some(r) => r,
        };
        let pool = top_k.max(cfg().rerank_pool as usize);
        let (chunks, timing) = self.search_rrf_docs(query, pool, folder, corpus, 0, docs)?;
        let mut timing = meta_timing(timing, mp.as_ref());
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
        // 관련도 문턱은 리랭커 점수 그대로로 본다(가산 때문에 관련 없는 근거가 문턱을 넘지 않게)
        let top = scores.iter().cloned().fold(f32::MIN, f32::max) as f64;
        let mut scores: Vec<f64> = scores.iter().map(|s| *s as f64).collect();
        if let Some(p) = mp.as_ref().filter(|p| !p.boosts.is_empty()) {
            // 메타데이터 가산 — 필터로 쓰기엔 애매한 조건(작성 역할 · 유형 · 양식 · 기간 안)으로 순위만 올린다(§13-1)
            for (s, c) in scores.iter_mut().zip(chunks.iter()) {
                *s += p.boosts.get(&c.doc_path).copied().unwrap_or(0.0);
            }
        }
        let mut order: Vec<usize> = (0..chunks.len()).collect();
        order.sort_by(|a, b| scores[*b].partial_cmp(&scores[*a]).unwrap_or(std::cmp::Ordering::Equal));
        timing.insert("rerank_ms".into(), pyutil::jf(rms));
        let total = pyutil::round_to(tget(&timing, "total_ms") + rms, 1);
        timing.insert("total_ms".into(), pyutil::jf(total));
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
        let mut picked = if cfg().dedup_evidence { pick_unique(ranked, top_k) } else { ranked.into_iter().take(top_k).collect() };
        // 동의어로 넓힌 질문이면 RRF 1위를 잃지 않게 한 건 더 붙인다 — 리랭커는 일상어 질문 그대로 보므로
        //   사전이 끌어올린 조항을 다시 밀어낸다(바꿔 말한 질문 42문항: 29 → 37, 골든 100문항 85 그대로)
        if let Some(extra) = synonym_extra(&picked, &chunks[0], &timing, top_k) {
            picked.push(extra);
            timing.insert("synonym_extra".into(), json!(true));
        }
        // 정밀 모드면 근거마다 후보 안의 앞뒤 이웃 청크를 잇는다(4B 와 함께 골든 100문항 50 → 59)
        let (picked, n_nb) = attach_neighbors(picked, &chunks);
        if n_nb > 0 {
            timing.insert("neighbors".into(), json!(n_nb));
        }
        Ok((picked, timing))
    }

    //------------------------------------------------------------------
    // 1단계 — 질의 임베딩 → dense·BM25 후보 → RRF → 사본 거르기 → top_k
    //------------------------------------------------------------------
    pub fn search_rrf(&self, query: &str, top_k: usize, folder: Option<&str>) -> Result<(Vec<Chunk>, Timing), String> {
        self.search_rrf_with(query, top_k, folder, None)
    }

    //------------------------------------------------------------------
    // 1단계 — corpus 를 주면 sqlite 벡터·BM25 캐시 대신 그 대화의 청크로(전수 내적 + 메모리 BM25)
    //------------------------------------------------------------------
    pub fn search_rrf_with(&self, query: &str, top_k: usize, folder: Option<&str>,
                           corpus: Option<&crate::uploads::UploadCorpus>) -> Result<(Vec<Chunk>, Timing), String> {
        self.search_rrf_k(query, top_k, folder, corpus, 0)
    }

    //------------------------------------------------------------------
    // 1단계 — wide 를 주면 dense·BM25 후보 수를 그만큼 넓힌다(설정값보다 작으면 설정값)
    //   탐색기 모드는 "넓게" 찾아 문서별로 묶으므로 후보가 설정값(답용)보다 많아야 한다(find.rs)
    //------------------------------------------------------------------
    pub fn search_rrf_k(&self, query: &str, top_k: usize, folder: Option<&str>,
                        corpus: Option<&crate::uploads::UploadCorpus>, wide: usize) -> Result<(Vec<Chunk>, Timing), String> {
        self.search_rrf_docs(query, top_k, folder, corpus, wide, None)
    }

    //------------------------------------------------------------------
    // 1단계 — docs 를 주면 그 문서들(메타데이터 조건에 맞는 문서)에서만 찾는다
    //   폴더 한정과 같은 허용 점 id 방식(메타데이터 검색 설계서 §13-1)
    //------------------------------------------------------------------
    pub fn search_rrf_docs(&self, query: &str, top_k: usize, folder: Option<&str>,
                           corpus: Option<&crate::uploads::UploadCorpus>, wide: usize,
                           docs: Option<&HashSet<String>>) -> Result<(Vec<Chunk>, Timing), String> {
        let dense_k = (cfg().dense_top_k as usize).max(wide);
        let bm25_k = if cfg().bm25_top_k > 0 { (cfg().bm25_top_k as usize).max(wide) } else { 0 };
        let t0 = Instant::now();
        let scope = match (docs, folder) {
            (Some(d), f) if corpus.is_none() => Some(self.store.docs_scope(d, f)?),
            (_, Some(f)) => Some(self.store.folder_scope(f)?),
            _ => None,
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
        // 업로드 문서면 대화의 청크 벡터 전부와 내적(수백 청크라 1ms 안팎)
        let dense = match corpus {
            Some(c) => c.search(&qv, dense_k),
            None => self.store.search(&qv, dense_k, allow)?,
        };
        let dense_ids: Vec<i64> = dense.iter().map(|d| d.0).collect();
        let mut payloads: HashMap<i64, Payload> = dense.into_iter().map(|(i, _, p)| (i, p)).collect();
        let t2 = Instant::now();
        let tok = self.embedder.tokenizer()?;
        // 동의어 사전으로 BM25 질의만 넓힌다 — 일상어("할아버지")에 문서 용어("조부모")를 덧붙인다
        let (bm_query, syn_pairs) = crate::synonyms::expand(query);
        let bm25 = corpus.map(|c| &c.bm25).unwrap_or(&self.bm25);
        let bm_ids = if bm25_k > 0 { bm25.search(&bm_query, &tok, bm25_k, allow) } else { vec![] };
        let t3 = Instant::now();
        let mut lists = vec![dense_ids];
        if !bm_ids.is_empty() {
            lists.push(bm_ids);
        }
        let fused: Vec<i64> = rrf(&lists).into_iter().map(|(i, _)| i).collect();
        let missing: Vec<i64> = fused.iter().copied().filter(|i| !payloads.contains_key(i)).collect();
        if !missing.is_empty() {
            match corpus {
                Some(c) => payloads.extend(missing.iter().filter_map(|i| c.payloads.get(i).map(|p| (*i, p.clone())))),
                None => payloads.extend(self.store.payloads(&missing)?),
            }
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
        if !syn_pairs.is_empty() {
            // 동의어로 넓힌 내역 — 소요 블록에 "검색어 넓힘: 할아버지→조부모" 로 보여 준다
            timing.insert("synonyms".into(), json!(syn_pairs.iter().map(|(w, t)| format!("{}→{}", w, t)).collect::<Vec<_>>()));
        }
        if let Some((n, _)) = scope {
            timing.insert("scope_docs".into(), json!(n));
            timing.insert("scope_via".into(), json!("matrix"));
        }
        if let Some(c) = corpus {
            timing.insert("uploads".into(), json!(c.n_files));
        }
        Ok((chunks, timing))
    }
}

//------------------------------------------------------------------
// 메타데이터 계획 — 인덱서가 meta.sqlite 를 두는 곳(상태 파일 옆)의 사전 · 규칙으로
//   메타가 없거나 조건이 없으면 None(검색을 막지 않는다)
//------------------------------------------------------------------
pub fn meta_plan(query: &str, folder: Option<&str>) -> Option<crate::meta::query::Plan> {
    meta_plan_opts(query, folder, &crate::meta::query::PlanOpts::default())
}

// 메타데이터 계획 — 화면에서 끈 조건 · 넓힌 기간과 함께(탐색기 모드 조건 칩)
pub fn meta_plan_opts(query: &str, folder: Option<&str>, opts: &crate::meta::query::PlanOpts) -> Option<crate::meta::query::Plan> {
    let home = crate::meta::hook::data_dir();
    crate::meta::query::plan_with(query, &crate::meta::store::default_path(&home), &home, folder, None, opts)
}

// timing 에 메타데이터 해석 결과를 싣는다 — 화면이 조건 칩 · "조건을 풀어 찾음" 을 보인다(§14)
pub fn meta_timing(mut timing: Timing, mp: Option<&crate::meta::query::Plan>) -> Timing {
    if let Some(p) = mp {
        timing.insert("meta".into(), p.to_json());
    }
    timing
}

impl Retriever {
    //------------------------------------------------------------------
    // 본문 검색어가 남지 않은 질의 — 조건에 맞는 문서를 근거로 (§13-2)
    //=> "작년에 고봉수님이 만든 문서" 처럼 조건만 있으면 본문 검색을 하지 않는다. 계획의 순서(가산 · 수정일)로
    //   top_k 개 골라 문서마다 첫 청크를 근거로 돌려준다. 본문이 없는 문서(스캔 PDF 등)는 이름만 적은 근거.
    //------------------------------------------------------------------
    pub fn meta_only(&self, mp: &crate::meta::query::Plan, top_k: usize) -> Result<(Vec<Chunk>, Timing), String> {
        let t0 = Instant::now();
        let state = crate::indexer::load_state();
        let docs = crate::indexer::docs(&state);
        let picked: Vec<&String> = mp.order.iter().take(top_k).collect();
        let first_ids: Vec<i64> = picked.iter()
            .filter_map(|k| docs.get(*k).and_then(|d| d.get("ids")).and_then(|v| v.as_array()).and_then(|a| a.first()).and_then(|v| v.as_i64()))
            .collect();
        let payloads = if first_ids.is_empty() { HashMap::new() } else { self.store.payloads(&first_ids)? };
        let by_path: HashMap<String, (i64, Payload)> = payloads.into_iter().filter(|(_, p)| !p.is_empty())
            .map(|(i, p)| (pl_str(&p, "doc_path").to_string(), (i, p))).collect();
        let chunks: Vec<Chunk> = picked.iter().map(|k| {
            let name = pyutil::basename(k).to_string();
            match by_path.get(*k) {
                Some((i, pl)) => Chunk {
                    id: *i, text: pl_str(pl, "text").to_string(), doc_name: pl_str(pl, "doc_name").to_string(),
                    doc_path: (*k).clone(), folder: pl_str(pl, "folder").to_string(),
                    chunk_idx: pl.get("chunk_idx").and_then(|v| v.as_i64()).unwrap_or(0),
                    section_id: pl.get("section_id").and_then(|v| v.as_i64()), child_text: None,
                },
                None => Chunk {
                    id: -1, text: format!("[{}] (본문을 읽지 못한 문서 — 이름 · 속성으로 찾음)", name), doc_name: name,
                    doc_path: (*k).clone(), folder: String::new(), chunk_idx: 0, section_id: None, child_text: None,
                },
            }
        }).collect();
        let mut timing = Timing::new();
        for k in ["embed_ms", "dense_ms", "bm25_ms", "fuse_ms"] {
            timing.insert(k.into(), json!(0.0));
        }
        timing.insert("total_ms".into(), pyutil::jf(pyutil::round_to(ms(t0.elapsed()) + mp.ms, 1)));
        Ok((chunks, meta_timing(timing, Some(mp))))
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
