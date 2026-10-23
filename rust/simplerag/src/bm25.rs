//------------------------------------------------------------------
// BM25 역색인 (Python index/bm25.py)
//=> 하이브리드 검색의 나머지 절반 — 한국어 고유명사·조문번호·금액 같은 표층 일치를 잡는다.
//   토큰은 반드시 e5 서브워드(공백 분리는 조사 때문에 Recall 이 크게 떨어진다).
//    · 구축: 문서별 토큰 빈도 → (term, doc) postings 를 term 순으로 모으고 가중치를 미리 계산
//         w(d,t) = idf(t) · tf · (k1+1) / (tf + k1·(1 − b + b·dl/avgdl)),  idf = ln((N−df+.5)/(df+.5) + 1)
//    · 질의: 질의 토큰(중복 제거)의 postings 만 더한다
//   저장 형식은 Rust 판 전용 바이너리(bm25_index.bin).
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::sync::Mutex;
use std::time::Instant;

use crate::config::cfg;
use crate::tok::Tok;

const MAGIC: &[u8; 8] = b"SRBM25\0\x02";

#[derive(Default)]
struct Index {
    vocab: HashMap<String, u32>,
    term_offset: Vec<u64>,
    post_doc: Vec<u32>,
    post_w: Vec<f32>,
    ids: Vec<i64>,
}

//------------------------------------------------------------------
// BM25 색인 (지연 적재)
//------------------------------------------------------------------
pub struct Bm25Index {
    pub path: String,
    idx: Mutex<Option<Index>>,
}

impl Default for Bm25Index {
    fn default() -> Self {
        Self::new(None)
    }
}

impl Bm25Index {
    pub fn new(path: Option<String>) -> Bm25Index {
        Bm25Index { path: path.unwrap_or_else(|| cfg().bm25_path.clone()), idx: Mutex::new(None) }
    }

    pub fn is_ready(&self) -> bool {
        self.idx.lock().unwrap().is_some()
    }

    //------------------------------------------------------------------
    // 구축 (핵심) — 걸린 초
    // -in: chunks = 청크 본문 / ids = 각 청크의 점 id / tok = e5 토크나이저
    //------------------------------------------------------------------
    pub fn build(&self, chunks: &[String], ids: &[i64], tok: &Tok) -> Result<f64, String> {
        let t0 = Instant::now();
        let n_docs = chunks.len();
        let mut vocab: HashMap<String, u32> = HashMap::new();
        let mut rows: Vec<(u32, u32, f32)> = vec![]; // (term, doc, tf)
        let mut doc_len = vec![0f32; n_docs];
        for (d, text) in chunks.iter().enumerate() {
            // 문서 안 첫 등장 순서를 지키며 빈도를 센다
            let mut order: Vec<String> = vec![];
            let mut counts: HashMap<String, u32> = HashMap::new();
            for t in tok.tokens(text) {
                let e = counts.entry(t.clone()).or_insert(0);
                if *e == 0 {
                    order.push(t);
                }
                *e += 1;
            }
            doc_len[d] = counts.values().sum::<u32>() as f32;
            for t in order {
                let tf = counts[&t];
                let next = vocab.len() as u32;
                let tid = *vocab.entry(t).or_insert(next);
                rows.push((tid, d as u32, tf as f32));
            }
        }
        let n_terms = vocab.len();
        rows.sort_by_key(|r| r.0); // 안정 정렬 — term 안에서는 문서 순서
        let mut df = vec![0f32; n_terms];
        for r in &rows {
            df[r.0 as usize] += 1.0;
        }
        let mut term_offset = vec![0u64; n_terms + 1];
        for t in 0..n_terms {
            term_offset[t + 1] = term_offset[t] + df[t] as u64;
        }
        let avgdl = if n_docs > 0 { doc_len.iter().sum::<f32>() / n_docs as f32 } else { 1.0 };
        let n = n_docs as f32;
        let idf: Vec<f32> = df.iter().map(|&d| ((n - d + 0.5) / (d + 0.5) + 1.0).ln()).collect();
        let (k1, b) = (cfg().bm25_k1 as f32, cfg().bm25_b as f32);
        let mut post_doc = Vec::with_capacity(rows.len());
        let mut post_w = Vec::with_capacity(rows.len());
        for (t, d, tf) in &rows {
            let dl = doc_len[*d as usize];
            let denom = tf + k1 * (1.0 - b + b * dl / avgdl.max(1e-9));
            post_doc.push(*d);
            post_w.push(idf[*t as usize] * tf * (k1 + 1.0) / denom);
        }
        let idx = Index { vocab, term_offset, post_doc, post_w, ids: ids.to_vec() };
        save(&self.path, &idx).map_err(|e| format!("BM25 저장 실패: {}", e))?;
        *self.idx.lock().unwrap() = Some(idx);
        Ok(t0.elapsed().as_secs_f64())
    }

    //------------------------------------------------------------------
    // 캐시에서 적재 — 걸린 ms, 없거나 깨졌으면 -1
    //------------------------------------------------------------------
    pub fn ensure_loaded(&self) -> f64 {
        let mut g = self.idx.lock().unwrap();
        if g.is_some() {
            return 0.0;
        }
        if !std::path::Path::new(&self.path).is_file() {
            return -1.0;
        }
        let t0 = Instant::now();
        match load(&self.path) {
            Ok(i) => {
                *g = Some(i);
                t0.elapsed().as_secs_f64() * 1000.0
            }
            Err(_) => -1.0,
        }
    }

    //------------------------------------------------------------------
    // 검색 — 점 id 목록(점수 순, 0점 제외)
    // -in: allow = Some(허용 id) 이면 그 밖은 0점(폴더 한정)
    //------------------------------------------------------------------
    pub fn search(&self, query: &str, tok: &Tok, limit: usize, allow: Option<&HashSet<i64>>) -> Vec<i64> {
        if self.ensure_loaded() < 0.0 {
            return vec![];
        }
        let g = self.idx.lock().unwrap();
        let idx = match g.as_ref() {
            Some(i) => i,
            None => return vec![],
        };
        let n_docs = idx.ids.len();
        let mut scores = vec![0f32; n_docs];
        let mut seen = HashSet::new();
        for t in tok.tokens(query) {
            if !seen.insert(t.clone()) {
                continue;
            }
            let tid = match idx.vocab.get(&t) {
                Some(t) => *t as usize,
                None => continue,
            };
            let (s, e) = (idx.term_offset[tid] as usize, idx.term_offset[tid + 1] as usize);
            for k in s..e {
                scores[idx.post_doc[k] as usize] += idx.post_w[k];
            }
        }
        if let Some(a) = allow {
            for (i, sc) in scores.iter_mut().enumerate() {
                if !a.contains(&idx.ids[i]) {
                    *sc = 0.0;
                }
            }
        }
        let mut order: Vec<usize> = (0..n_docs).filter(|&i| scores[i] > 0.0).collect();
        order.sort_by(|a, b| scores[*b].partial_cmp(&scores[*a]).unwrap_or(std::cmp::Ordering::Equal).then(a.cmp(b)));
        order.truncate(limit.min(n_docs));
        order.into_iter().map(|i| idx.ids[i]).collect()
    }
}

// 바이너리 저장 — 임시 파일에 쓰고 바꿔치기
fn save(path: &str, idx: &Index) -> std::io::Result<()> {
    if let Some(d) = std::path::Path::new(path).parent() {
        std::fs::create_dir_all(d)?;
    }
    let mut b: Vec<u8> = Vec::new();
    b.extend_from_slice(MAGIC);
    let mut terms: Vec<(&String, &u32)> = idx.vocab.iter().collect();
    terms.sort_by_key(|t| *t.1);
    b.extend_from_slice(&(terms.len() as u64).to_le_bytes());
    for (t, _) in &terms {
        b.extend_from_slice(&(t.len() as u32).to_le_bytes());
        b.extend_from_slice(t.as_bytes());
    }
    for x in &idx.term_offset {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b.extend_from_slice(&(idx.post_doc.len() as u64).to_le_bytes());
    for x in &idx.post_doc {
        b.extend_from_slice(&x.to_le_bytes());
    }
    for x in &idx.post_w {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b.extend_from_slice(&(idx.ids.len() as u64).to_le_bytes());
    for x in &idx.ids {
        b.extend_from_slice(&x.to_le_bytes());
    }
    let tmp = format!("{}.tmp", path);
    std::fs::File::create(&tmp)?.write_all(&b)?;
    std::fs::rename(&tmp, path)
}

// 바이너리 읽기
fn load(path: &str) -> Result<Index, String> {
    let mut b = vec![];
    std::fs::File::open(path).and_then(|mut f| f.read_to_end(&mut b)).map_err(|e| e.to_string())?;
    if b.len() < 8 || &b[..8] != MAGIC {
        return Err("형식 다름".into());
    }
    let mut p = 8usize;
    let mut take = |n: usize| -> Result<&[u8], String> {
        let s = b.get(p..p + n).ok_or("잘림")?;
        p += n;
        Ok(s)
    };
    let u64v = |s: &[u8]| u64::from_le_bytes(s.try_into().unwrap());
    let n_terms = u64v(take(8)?) as usize;
    let mut vocab = HashMap::with_capacity(n_terms);
    for t in 0..n_terms {
        let l = u32::from_le_bytes(take(4)?.try_into().unwrap()) as usize;
        let s = String::from_utf8(take(l)?.to_vec()).map_err(|e| e.to_string())?;
        vocab.insert(s, t as u32);
    }
    let mut term_offset = Vec::with_capacity(n_terms + 1);
    for _ in 0..=n_terms {
        term_offset.push(u64v(take(8)?));
    }
    let nnz = u64v(take(8)?) as usize;
    let mut post_doc = Vec::with_capacity(nnz);
    for _ in 0..nnz {
        post_doc.push(u32::from_le_bytes(take(4)?.try_into().unwrap()));
    }
    let mut post_w = Vec::with_capacity(nnz);
    for _ in 0..nnz {
        post_w.push(f32::from_le_bytes(take(4)?.try_into().unwrap()));
    }
    let n_ids = u64v(take(8)?) as usize;
    let mut ids = Vec::with_capacity(n_ids);
    for _ in 0..n_ids {
        ids.push(i64::from_le_bytes(take(8)?.try_into().unwrap()));
    }
    Ok(Index { vocab, term_offset, post_doc, post_w, ids })
}
