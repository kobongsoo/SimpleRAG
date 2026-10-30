//------------------------------------------------------------------
// BM25 역색인 — 세그먼트형 (Python index/bm25.py · 인덱싱 속도 개선 설계서 §4-4)
//=> 하이브리드 검색의 나머지 절반 — 한국어 고유명사·조문번호·금액 같은 표층 일치를 잡는다.
//   토큰은 반드시 e5 서브워드(공백 분리는 조사 때문에 Recall 이 크게 떨어진다).
//    · 예전에는 가중치를 미리 계산해 저장했다. 가중치에 전체 통계(N · 평균 길이 · df)가 들어가므로
//      문서 1건만 바뀌어도 18.7만 청크를 통째로 다시 만들어야 했다(운영 36초).
//    · 지금은 단어 빈도(tf)만 저장하고 가중치는 질의 때 계산한다 — 통계가 바뀌어도 다시 만들 필요가 없다.
//         w(d,t) = idf(t) · tf · (k1+1) / (tf + k1·(1 − b + b·dl/avgdl)),  idf = ln((N−df+.5)/(df+.5) + 1)
//    · 본 세그먼트(전체 인덱싱 때 만듦) + 덧붙임 세그먼트(그 뒤 새 청크) + 지움 표시(지운 청크)
//      문서 추가·삭제는 덧붙임·지움 표시만 고친다(ms 단위). 덧붙임이나 지움이 본의 5% 를 넘으면 합친다.
//    · df 는 지운 청크를 빼고 세므로 점수가 전체 재구축과 같다.
//   저장 형식은 Rust 판 전용 바이너리 — 본: bm25_index.bin, 덧붙임·지움: bm25_index.bin.delta
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::sync::Mutex;
use std::time::Instant;

use crate::config::cfg;
use crate::tok::Tok;

// 형식이 바뀌면 올린다 — v2(가중치 미리 계산)는 읽지 않고 처음 한 번 새로 만든다
const MAGIC: &[u8; 8] = b"SRBM25\0\x03";
const DELTA_MAGIC: &[u8; 8] = b"SRBM25d\x03";

// 덧붙임·지움이 본 세그먼트의 이 비율을 넘으면 합친다(전체 다시 만들기)
pub const MERGE_RATIO: f64 = 0.05;

// 청크 하나의 단어별 빈도 — 첫 등장 순서
pub type Tf = Vec<(String, u32)>;

//------------------------------------------------------------------
// 단어 빈도 → 바이트 (저장소 bm25_tf 표에 넣는 모양)
//=> [단어 수 u32] 다음 단어마다 [글자 길이 u16][글자][빈도 u32]
//
// -in: tf = [(단어, 빈도)]
//
// -out: 바이트
// -out: error = 없음
//------------------------------------------------------------------
pub fn tf_to_blob(tf: &Tf) -> Vec<u8> {
    let mut b = Vec::with_capacity(4 + tf.len() * 12);
    b.extend_from_slice(&(tf.len() as u32).to_le_bytes());
    for (t, n) in tf {
        // 서브워드 한 개라 u16 길이를 넘지 않는다 — 넘으면 잘라서라도 형식을 지킨다
        let s = &t.as_bytes()[..t.len().min(u16::MAX as usize)];
        b.extend_from_slice(&(s.len() as u16).to_le_bytes());
        b.extend_from_slice(s);
        b.extend_from_slice(&n.to_le_bytes());
    }
    b
}

//------------------------------------------------------------------
// 바이트 → 단어 빈도 (tf_to_blob 의 반대)
//
// -in: b = 바이트
//
// -out: Some(tf) — 깨졌으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn tf_from_blob(b: &[u8]) -> Option<Tf> {
    let mut p = 0usize;
    let n = u32::from_le_bytes(b.get(0..4)?.try_into().ok()?) as usize;
    p += 4;
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let l = u16::from_le_bytes(b.get(p..p + 2)?.try_into().ok()?) as usize;
        p += 2;
        let s = String::from_utf8(b.get(p..p + l)?.to_vec()).ok()?;
        p += l;
        let c = u32::from_le_bytes(b.get(p..p + 4)?.try_into().ok()?);
        p += 4;
        out.push((s, c));
    }
    Some(out)
}

//------------------------------------------------------------------
// 세그먼트 하나 — 단어 → (청크 행, tf) 목록
// -필드: vocab       = 단어 → 단어 번호(첫 등장 순서)
// -필드: term_offset = 단어 번호별 postings 구간 [시작, 끝)
// -필드: post_row    = postings 의 청크 행
// -필드: post_tf     = postings 의 단어 빈도
// -필드: doc_len     = 행별 청크 길이(토큰 수)
// -필드: ids         = 행 → 점 id
//------------------------------------------------------------------
#[derive(Default)]
struct Seg {
    vocab: HashMap<String, u32>,
    term_offset: Vec<u64>,
    post_row: Vec<u32>,
    post_tf: Vec<u32>,
    doc_len: Vec<u32>,
    ids: Vec<i64>,
}

impl Seg {
    // 단어의 postings 구간
    fn range(&self, term: &str) -> Option<(usize, usize)> {
        let t = *self.vocab.get(term)? as usize;
        Some((self.term_offset[t] as usize, self.term_offset[t + 1] as usize))
    }
}

//------------------------------------------------------------------
// 단어 빈도들로 세그먼트 만들기 (핵심)
//=> 예전 build() 와 같은 순서로 만든다 — 그래서 같은 입력이면 파일이 바이트까지 같다.
//    1) 청크 차례로, 청크 안 첫 등장 순서로 단어 번호를 매긴다
//    2) (단어, 행, tf) 를 단어 번호로 안정 정렬해 postings 를 연속 구간으로 만든다
//
// -in: ids = 점 id 목록 / tfs = 같은 순서의 단어 빈도
//
// -out: 세그먼트
// -out: error = 없음
//------------------------------------------------------------------
fn make_seg(ids: &[i64], tfs: &[&Tf]) -> Seg {
    let mut vocab: HashMap<String, u32> = HashMap::new();
    let mut rows: Vec<(u32, u32, u32)> = vec![]; // (term, row, tf)
    let mut doc_len = vec![0u32; ids.len()];
    for (d, tf) in tfs.iter().enumerate() {
        doc_len[d] = tf.iter().map(|x| x.1).sum();
        for (t, n) in tf.iter() {
            let next = vocab.len() as u32;
            let tid = match vocab.get(t) {
                Some(i) => *i,
                None => {
                    vocab.insert(t.clone(), next);
                    next
                }
            };
            rows.push((tid, d as u32, *n));
        }
    }
    let n_terms = vocab.len();
    // 안정 정렬 — 단어 안에서는 청크 순서
    rows.sort_by_key(|r| r.0);
    let mut term_offset = vec![0u64; n_terms + 1];
    for r in &rows {
        term_offset[r.0 as usize + 1] += 1;
    }
    for t in 0..n_terms {
        term_offset[t + 1] += term_offset[t];
    }
    Seg {
        vocab,
        term_offset,
        post_row: rows.iter().map(|r| r.1).collect(),
        post_tf: rows.iter().map(|r| r.2).collect(),
        doc_len,
        ids: ids.to_vec(),
    }
}

//------------------------------------------------------------------
// 메모리 색인 — 본 + 덧붙임 + 지움 표시 + 전체 통계
// -필드: stamp     = 본 세그먼트를 만든 시각 표식(덧붙임 파일이 같은 본에 속하는지 확인)
// -필드: row_of    = 본 세그먼트 점 id → 행
// -필드: deleted   = 본 세그먼트 행별 지움 표시
// -필드: add_tf    = 덧붙임 청크들의 (id, tf) — id 순
// -필드: n_live / len_sum = 살아 있는 청크 수 · 길이 합(N · 평균 길이 계산용)
//------------------------------------------------------------------
struct Index {
    stamp: u64,
    base: Seg,
    row_of: HashMap<i64, u32>,
    deleted: Vec<bool>,
    n_deleted: usize,
    add_tf: Vec<(i64, Tf)>,
    add: Seg,
    n_live: usize,
    len_sum: u64,
}

impl Index {
    //------------------------------------------------------------------
    // 본 세그먼트 + 덧붙임 · 지움 목록으로 색인 만들기
    //
    // -in: stamp = 본 표식 / base = 본 세그먼트 / deleted_ids = 지운 본 청크 id / add_tf = 덧붙임 (id, tf)
    //
    // -out: 색인
    // -out: error = 없음
    //------------------------------------------------------------------
    fn new(stamp: u64, base: Seg, deleted_ids: &[i64], add_tf: Vec<(i64, Tf)>) -> Index {
        let row_of: HashMap<i64, u32> = base.ids.iter().enumerate().map(|(r, id)| (*id, r as u32)).collect();
        let mut deleted = vec![false; base.ids.len()];
        for id in deleted_ids {
            if let Some(&r) = row_of.get(id) {
                deleted[r as usize] = true;
            }
        }
        let mut idx = Index { stamp, base, row_of, deleted, n_deleted: 0, add_tf, add: Seg::default(), n_live: 0, len_sum: 0 };
        idx.rebuild_add();
        idx
    }

    // 덧붙임 세그먼트를 add_tf 로 다시 만들고 통계를 센다(덧붙임은 작아서 ms 단위)
    fn rebuild_add(&mut self) {
        self.add_tf.sort_by_key(|x| x.0);
        let ids: Vec<i64> = self.add_tf.iter().map(|x| x.0).collect();
        let tfs: Vec<&Tf> = self.add_tf.iter().map(|x| &x.1).collect();
        self.add = make_seg(&ids, &tfs);
        self.n_deleted = self.deleted.iter().filter(|d| **d).count();
        self.n_live = self.base.ids.len() - self.n_deleted + self.add.ids.len();
        let base_len: u64 = self.base.doc_len.iter().zip(&self.deleted).filter(|(_, d)| !**d).map(|(l, _)| *l as u64).sum();
        self.len_sum = base_len + self.add.doc_len.iter().map(|l| *l as u64).sum::<u64>();
    }

    // 지금 색인에 살아 있는 점 id
    fn live_ids(&self) -> HashSet<i64> {
        let mut s: HashSet<i64> = self.base.ids.iter().zip(&self.deleted).filter(|(_, d)| !**d).map(|(i, _)| *i).collect();
        s.extend(self.add.ids.iter().copied());
        s
    }
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

// 덧붙임 · 지움 파일 경로
fn delta_path(path: &str) -> String {
    format!("{}.delta", path)
}

// 본 표식 — 만든 시각(나노초)
fn new_stamp() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(1)
}

impl Bm25Index {
    pub fn new(path: Option<String>) -> Bm25Index {
        Bm25Index { path: path.unwrap_or_else(|| cfg().bm25_path.clone()), idx: Mutex::new(None) }
    }

    pub fn is_ready(&self) -> bool {
        self.idx.lock().unwrap().is_some()
    }

    //------------------------------------------------------------------
    // 구축 — 본문에서 (토큰화 포함) — 걸린 초
    //=> 토큰화는 스레드 여러 개로 나눈다(단계 A). 업로드 문서처럼 저장소 토큰 표가 없는 곳이 쓴다.
    //
    // -in: chunks = 청크 본문 / ids = 각 청크의 점 id / tok = e5 토크나이저
    //
    // -out: 걸린 초
    // -out: error = 파일 저장 실패면 Err
    //------------------------------------------------------------------
    pub fn build(&self, chunks: &[String], ids: &[i64], tok: &Tok) -> Result<f64, String> {
        let t0 = Instant::now();
        let texts: Vec<&str> = chunks.iter().map(|s| s.as_str()).collect();
        let tfs = tok.term_freqs_par(&texts, crate::config::bm25_threads());
        self.build_tf(ids, &tfs)?;
        Ok(t0.elapsed().as_secs_f64())
    }

    //------------------------------------------------------------------
    // 구축 — 단어 빈도에서 (전체 다시 만들기 · 합치기) — 걸린 초
    //=> 본 세그먼트를 새로 만들고 덧붙임 · 지움은 비운다.
    //
    // -in: ids = 점 id 목록 / tfs = 같은 순서의 단어 빈도
    //
    // -out: 걸린 초
    // -out: error = 파일 저장 실패면 Err("BM25 저장 실패: …")
    //------------------------------------------------------------------
    pub fn build_tf(&self, ids: &[i64], tfs: &[Tf]) -> Result<f64, String> {
        let t0 = Instant::now();
        let refs: Vec<&Tf> = tfs.iter().collect();
        let base = make_seg(ids, &refs);
        let stamp = new_stamp();
        save_base(&self.path, stamp, &base).map_err(|e| format!("BM25 저장 실패: {}", e))?;
        // 옛 덧붙임 파일은 새 본에 다 들어갔다 — 남아 있어도 표식이 달라 무시되지만 지워 둔다
        let _ = std::fs::remove_file(delta_path(&self.path));
        *self.idx.lock().unwrap() = Some(Index::new(stamp, base, &[], vec![]));
        Ok(t0.elapsed().as_secs_f64())
    }

    //------------------------------------------------------------------
    // 무엇을 고쳐야 하나 — (넣을 id, 뺄 id), 색인이 없으면 None
    //=> 저장소에 지금 있는 점과 색인에 든 점을 맞대어 본다. id 는 다시 쓰지 않으므로 id 만 보면 된다.
    //
    // -in: live = 저장소의 점 id 목록
    //
    // -out: Some((넣을 id 오름차순, 뺄 id 오름차순)) / 색인을 못 읽으면 None
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn plan_update(&self, live: &[i64]) -> Option<(Vec<i64>, Vec<i64>)> {
        if self.ensure_loaded() < 0.0 {
            return None;
        }
        let g = self.idx.lock().unwrap();
        let idx = g.as_ref()?;
        let have = idx.live_ids();
        let live_set: HashSet<i64> = live.iter().copied().collect();
        let mut add: Vec<i64> = live.iter().copied().filter(|i| !have.contains(i)).collect();
        let mut rem: Vec<i64> = have.into_iter().filter(|i| !live_set.contains(i)).collect();
        add.sort();
        rem.sort();
        Some((add, rem))
    }

    //------------------------------------------------------------------
    // 합칠 때가 됐나 — 덧붙임이나 지움이 본의 5% 를 넘으면
    //
    // -in: n_add / n_remove = 이번에 더할 · 뺄 청크 수
    //
    // -out: true 면 전체 다시 만들기(build_tf)
    // -out: error = 없음 (색인이 없으면 true)
    //------------------------------------------------------------------
    pub fn should_merge(&self, n_add: usize, n_remove: usize) -> bool {
        let g = self.idx.lock().unwrap();
        let idx = match g.as_ref() {
            Some(i) => i,
            None => return true,
        };
        let base = idx.base.ids.len() as f64;
        if base == 0.0 {
            return true;
        }
        (idx.add_tf.len() + n_add) as f64 > MERGE_RATIO * base || (idx.n_deleted + n_remove) as f64 > MERGE_RATIO * base
    }

    //------------------------------------------------------------------
    // 고치기 — 새 청크는 덧붙이고, 지운 청크는 표시만 (단계 C) — 걸린 초
    //    1) 뺄 id: 본에 있으면 지움 표시, 덧붙임에 있으면 덧붙임에서 뺀다
    //    2) 넣을 id: 덧붙임에 넣는다(같은 id 가 있으면 바꾼다)
    //    3) 덧붙임 세그먼트와 통계를 다시 세고 덧붙임 파일만 쓴다(본 파일은 그대로)
    //
    // -in: add = 넣을 (id, tf) / remove = 뺄 id
    //
    // -out: 걸린 초
    // -out: error = 색인이 없거나 파일 저장 실패면 Err
    //------------------------------------------------------------------
    pub fn update(&self, add: Vec<(i64, Tf)>, remove: &[i64]) -> Result<f64, String> {
        let t0 = Instant::now();
        if self.ensure_loaded() < 0.0 {
            return Err("BM25 색인이 없어 고칠 수 없습니다".into());
        }
        let mut g = self.idx.lock().unwrap();
        let idx = g.as_mut().ok_or("BM25 색인이 없어 고칠 수 없습니다")?;
        let gone: HashSet<i64> = remove.iter().copied().chain(add.iter().map(|x| x.0)).collect();
        for id in remove {
            if let Some(&r) = idx.row_of.get(id) {
                idx.deleted[r as usize] = true;
            }
        }
        // 덧붙임에서 빠질 것(지운 것 · 바꿀 것)을 뺀 뒤 새것을 넣는다
        idx.add_tf.retain(|x| !gone.contains(&x.0));
        idx.add_tf.extend(add);
        idx.rebuild_add();
        let deleted_ids: Vec<i64> = idx.base.ids.iter().zip(&idx.deleted).filter(|(_, d)| **d).map(|(i, _)| *i).collect();
        save_delta(&self.path, idx.stamp, &deleted_ids, &idx.add_tf).map_err(|e| format!("BM25 저장 실패: {}", e))?;
        Ok(t0.elapsed().as_secs_f64())
    }

    //------------------------------------------------------------------
    // 규모 — (본 청크 수, 덧붙임 청크 수, 지운 청크 수), 색인이 없으면 None
    //------------------------------------------------------------------
    pub fn counts(&self) -> Option<(usize, usize, usize)> {
        let g = self.idx.lock().unwrap();
        g.as_ref().map(|i| (i.base.ids.len(), i.add.ids.len(), i.n_deleted))
    }

    //------------------------------------------------------------------
    // 캐시에서 적재 — 걸린 ms, 없거나 깨졌거나 옛 형식이면 -1
    //=> 본 파일을 읽고, 같은 본에 속한 덧붙임 파일이 있으면 함께 읽는다.
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
        match load_base(&self.path) {
            Ok((stamp, base)) => {
                // 표식이 다르면 다른 본의 찌꺼기다 — 무시한다
                let (del, add) = load_delta(&delta_path(&self.path), stamp).unwrap_or_default();
                *g = Some(Index::new(stamp, base, &del, add));
                t0.elapsed().as_secs_f64() * 1000.0
            }
            Err(_) => -1.0,
        }
    }

    //------------------------------------------------------------------
    // 검색 — 점 id 목록(점수 순, 0점 제외)
    //=> 질의 단어마다 두 세그먼트의 postings 만 훑는다.
    //    1) df = 지우지 않은 postings 수(본) + 덧붙임 postings 수 → idf
    //    2) postings 마다 그 자리에서 가중치를 계산해 더한다(예전 미리 계산과 같은 식 · 같은 순서)
    //    3) 같은 점수는 id 가 작은 쪽이 먼저(예전 행 순서와 같다)
    // -in: allow = Some(허용 id) 이면 그 밖은 뺀다(폴더 한정)
    //------------------------------------------------------------------
    pub fn search(&self, query: &str, tok: &Tok, limit: usize, allow: Option<&HashSet<i64>>) -> Vec<i64> {
        self.search_scored(query, tok, limit, allow).into_iter().map(|h| h.0).collect()
    }

    //------------------------------------------------------------------
    // 검색 — (점 id, 점수) 점수 순 (search 의 본체 · 점수 동일성 시험용)
    //------------------------------------------------------------------
    pub fn search_scored(&self, query: &str, tok: &Tok, limit: usize, allow: Option<&HashSet<i64>>) -> Vec<(i64, f32)> {
        if self.ensure_loaded() < 0.0 {
            return vec![];
        }
        let g = self.idx.lock().unwrap();
        let idx = match g.as_ref() {
            Some(i) => i,
            None => return vec![],
        };
        if idx.n_live == 0 {
            return vec![];
        }
        let n = idx.n_live as f32;
        let avgdl = (idx.len_sum as f64 / idx.n_live as f64) as f32;
        let (k1, b) = (cfg().bm25_k1 as f32, cfg().bm25_b as f32);
        let mut base_sc = vec![0f32; idx.base.ids.len()];
        let mut add_sc = vec![0f32; idx.add.ids.len()];
        let mut seen = HashSet::new();
        for t in tok.tokens(query) {
            if !seen.insert(t.clone()) {
                continue;
            }
            let rb = idx.base.range(&t);
            let ra = idx.add.range(&t);
            // 지운 청크는 빼고 df 를 센다 — 그래야 전체 재구축과 점수가 같다
            let mut df = ra.map(|(s, e)| e - s).unwrap_or(0);
            if let Some((s, e)) = rb {
                df += (s..e).filter(|&k| !idx.deleted[idx.base.post_row[k] as usize]).count();
            }
            if df == 0 {
                continue;
            }
            let d = df as f32;
            let idf = ((n - d + 0.5) / (d + 0.5) + 1.0).ln();
            let weight = |tf: f32, dl: f32| {
                let denom = tf + k1 * (1.0 - b + b * dl / avgdl.max(1e-9));
                idf * tf * (k1 + 1.0) / denom
            };
            if let Some((s, e)) = rb {
                for k in s..e {
                    let r = idx.base.post_row[k] as usize;
                    if !idx.deleted[r] {
                        base_sc[r] += weight(idx.base.post_tf[k] as f32, idx.base.doc_len[r] as f32);
                    }
                }
            }
            if let Some((s, e)) = ra {
                for k in s..e {
                    let r = idx.add.post_row[k] as usize;
                    add_sc[r] += weight(idx.add.post_tf[k] as f32, idx.add.doc_len[r] as f32);
                }
            }
        }
        let ok = |id: &i64| allow.map(|a| a.contains(id)).unwrap_or(true);
        let mut hits: Vec<(f32, i64)> = base_sc.iter().enumerate().filter(|(r, s)| **s > 0.0 && ok(&idx.base.ids[*r]))
            .map(|(r, s)| (*s, idx.base.ids[r]))
            .chain(add_sc.iter().enumerate().filter(|(r, s)| **s > 0.0 && ok(&idx.add.ids[*r])).map(|(r, s)| (*s, idx.add.ids[r])))
            .collect();
        hits.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1)));
        hits.truncate(limit);
        hits.into_iter().map(|h| (h.1, h.0)).collect()
    }
}

// 임시 파일에 쓰고 바꿔치기
fn write_file(path: &str, b: &[u8]) -> std::io::Result<()> {
    if let Some(d) = std::path::Path::new(path).parent() {
        std::fs::create_dir_all(d)?;
    }
    let tmp = format!("{}.tmp", path);
    std::fs::File::create(&tmp)?.write_all(b)?;
    std::fs::rename(&tmp, path)
}

// 본 세그먼트 저장
fn save_base(path: &str, stamp: u64, s: &Seg) -> std::io::Result<()> {
    let mut b: Vec<u8> = Vec::new();
    b.extend_from_slice(MAGIC);
    b.extend_from_slice(&stamp.to_le_bytes());
    let mut terms: Vec<(&String, &u32)> = s.vocab.iter().collect();
    terms.sort_by_key(|t| *t.1);
    b.extend_from_slice(&(terms.len() as u64).to_le_bytes());
    for (t, _) in &terms {
        b.extend_from_slice(&(t.len() as u32).to_le_bytes());
        b.extend_from_slice(t.as_bytes());
    }
    for x in &s.term_offset {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b.extend_from_slice(&(s.post_row.len() as u64).to_le_bytes());
    for x in &s.post_row {
        b.extend_from_slice(&x.to_le_bytes());
    }
    for x in &s.post_tf {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b.extend_from_slice(&(s.ids.len() as u64).to_le_bytes());
    for x in &s.doc_len {
        b.extend_from_slice(&x.to_le_bytes());
    }
    for x in &s.ids {
        b.extend_from_slice(&x.to_le_bytes());
    }
    write_file(path, &b)
}

// 덧붙임 · 지움 저장 — [표식][지운 id 수][id…][덧붙임 수][(id, tf 바이트 길이, tf 바이트)…]
fn save_delta(path: &str, stamp: u64, deleted: &[i64], add: &[(i64, Tf)]) -> std::io::Result<()> {
    let mut b: Vec<u8> = Vec::new();
    b.extend_from_slice(DELTA_MAGIC);
    b.extend_from_slice(&stamp.to_le_bytes());
    b.extend_from_slice(&(deleted.len() as u64).to_le_bytes());
    for x in deleted {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b.extend_from_slice(&(add.len() as u64).to_le_bytes());
    for (id, tf) in add {
        let blob = tf_to_blob(tf);
        b.extend_from_slice(&id.to_le_bytes());
        b.extend_from_slice(&(blob.len() as u32).to_le_bytes());
        b.extend_from_slice(&blob);
    }
    write_file(&delta_path(path), &b)
}

// 앞에서부터 n 바이트씩 읽는 도우미
struct Reader<'a> {
    b: &'a [u8],
    p: usize,
}
impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], String> {
        let s = self.b.get(self.p..self.p + n).ok_or("잘림")?;
        self.p += n;
        Ok(s)
    }
    fn u32(&mut self) -> Result<u32, String> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Result<u64, String> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
    fn i64(&mut self) -> Result<i64, String> {
        Ok(i64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
}

fn read_all(path: &str) -> Result<Vec<u8>, String> {
    let mut b = vec![];
    std::fs::File::open(path).and_then(|mut f| f.read_to_end(&mut b)).map_err(|e| e.to_string())?;
    Ok(b)
}

// 본 세그먼트 읽기 — (표식, 세그먼트)
fn load_base(path: &str) -> Result<(u64, Seg), String> {
    let b = read_all(path)?;
    if b.len() < 8 || &b[..8] != MAGIC {
        return Err("형식 다름".into());
    }
    let mut r = Reader { b: &b, p: 8 };
    let stamp = r.u64()?;
    let n_terms = r.u64()? as usize;
    let mut vocab = HashMap::with_capacity(n_terms);
    for t in 0..n_terms {
        let l = r.u32()? as usize;
        let s = String::from_utf8(r.take(l)?.to_vec()).map_err(|e| e.to_string())?;
        vocab.insert(s, t as u32);
    }
    let mut term_offset = Vec::with_capacity(n_terms + 1);
    for _ in 0..=n_terms {
        term_offset.push(r.u64()?);
    }
    let nnz = r.u64()? as usize;
    let mut post_row = Vec::with_capacity(nnz);
    for _ in 0..nnz {
        post_row.push(r.u32()?);
    }
    let mut post_tf = Vec::with_capacity(nnz);
    for _ in 0..nnz {
        post_tf.push(r.u32()?);
    }
    let n_rows = r.u64()? as usize;
    let mut doc_len = Vec::with_capacity(n_rows);
    for _ in 0..n_rows {
        doc_len.push(r.u32()?);
    }
    let mut ids = Vec::with_capacity(n_rows);
    for _ in 0..n_rows {
        ids.push(r.i64()?);
    }
    Ok((stamp, Seg { vocab, term_offset, post_row, post_tf, doc_len, ids }))
}

// 덧붙임 · 지움 읽기 — 같은 본(표식)일 때만 Some((지운 id, 덧붙임))
fn load_delta(path: &str, stamp: u64) -> Option<(Vec<i64>, Vec<(i64, Tf)>)> {
    let b = read_all(path).ok()?;
    if b.len() < 16 || &b[..8] != DELTA_MAGIC {
        return None;
    }
    let mut r = Reader { b: &b, p: 8 };
    if r.u64().ok()? != stamp {
        return None;
    }
    let n_del = r.u64().ok()? as usize;
    let mut del = Vec::with_capacity(n_del);
    for _ in 0..n_del {
        del.push(r.i64().ok()?);
    }
    let n_add = r.u64().ok()? as usize;
    let mut add = Vec::with_capacity(n_add);
    for _ in 0..n_add {
        let id = r.i64().ok()?;
        let l = r.u32().ok()? as usize;
        add.push((id, tf_from_blob(r.take(l).ok()?)?));
    }
    Some((del, add))
}

#[cfg(test)]
mod tests {
    use super::*;

    // 작은 tf 만들기 — "a a b" → [(a,2),(b,1)]
    fn tf(words: &str) -> Tf {
        let mut out: Tf = vec![];
        for w in words.split_whitespace() {
            match out.iter_mut().find(|x| x.0 == w) {
                Some(x) => x.1 += 1,
                None => out.push((w.to_string(), 1)),
            }
        }
        out
    }

    // 점수 순 id 를 질의 단어 목록으로 직접 계산(토크나이저 없이 시험하려고 search 의 본체를 흉내)
    fn rank(bm: &Bm25Index, words: &[&str]) -> Vec<(i64, f32)> {
        let g = bm.idx.lock().unwrap();
        let idx = g.as_ref().unwrap();
        let n = idx.n_live as f32;
        let avgdl = (idx.len_sum as f64 / idx.n_live as f64) as f32;
        let (k1, b) = (1.5f32, 0.75f32);
        let mut sc: HashMap<i64, f32> = HashMap::new();
        for t in words {
            let rb = idx.base.range(t);
            let ra = idx.add.range(t);
            let mut df = ra.map(|(s, e)| e - s).unwrap_or(0);
            if let Some((s, e)) = rb {
                df += (s..e).filter(|&k| !idx.deleted[idx.base.post_row[k] as usize]).count();
            }
            if df == 0 {
                continue;
            }
            let d = df as f32;
            let idf = ((n - d + 0.5) / (d + 0.5) + 1.0).ln();
            let w = |tf: f32, dl: f32| idf * tf * (k1 + 1.0) / (tf + k1 * (1.0 - b + b * dl / avgdl));
            if let Some((s, e)) = rb {
                for k in s..e {
                    let r = idx.base.post_row[k] as usize;
                    if !idx.deleted[r] {
                        *sc.entry(idx.base.ids[r]).or_default() += w(idx.base.post_tf[k] as f32, idx.base.doc_len[r] as f32);
                    }
                }
            }
            if let Some((s, e)) = ra {
                for k in s..e {
                    let r = idx.add.post_row[k] as usize;
                    *sc.entry(idx.add.ids[r]).or_default() += w(idx.add.post_tf[k] as f32, idx.add.doc_len[r] as f32);
                }
            }
        }
        let mut v: Vec<(i64, f32)> = sc.into_iter().collect();
        v.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap().then(a.0.cmp(&b.0)));
        v
    }

    // 덧붙임 · 지움으로 고친 색인과 처음부터 다시 만든 색인의 점수가 같아야 한다(설계서 §6)
    #[test]
    fn segment_scores_equal_full_rebuild() {
        let dir = std::env::temp_dir().join(format!("bm25seg_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let p1 = dir.join("a.bin").to_string_lossy().into_owned();
        let p2 = dir.join("b.bin").to_string_lossy().into_owned();
        let docs: Vec<(i64, Tf)> = vec![
            (0, tf("휴가 규정 연차 연차")), (1, tf("출장 여비 규정")), (2, tf("연차 사용 촉진")),
            (3, tf("여비 정산 출장 출장")), (4, tf("규정 개정 이력")),
        ];
        let seg = Bm25Index::new(Some(p1.clone()));
        let ids: Vec<i64> = docs.iter().map(|d| d.0).collect();
        let tfs: Vec<Tf> = docs.iter().map(|d| d.1.clone()).collect();
        seg.build_tf(&ids, &tfs).unwrap();
        // 1 수정(지우고 새 id 5 로), 4 삭제, 6 추가
        seg.update(vec![(5, tf("출장 여비 규정 개정")), (6, tf("연차 휴가 신청"))], &[1, 4]).unwrap();
        // 같은 내용으로 처음부터
        let full = Bm25Index::new(Some(p2.clone()));
        let live: Vec<(i64, Tf)> = vec![(0, docs[0].1.clone()), (2, docs[2].1.clone()), (3, docs[3].1.clone()),
            (5, tf("출장 여비 규정 개정")), (6, tf("연차 휴가 신청"))];
        full.build_tf(&live.iter().map(|x| x.0).collect::<Vec<_>>(), &live.iter().map(|x| x.1.clone()).collect::<Vec<_>>()).unwrap();
        for q in [vec!["연차"], vec!["출장", "규정"], vec!["여비", "개정", "휴가"]] {
            let a = rank(&seg, &q);
            let b = rank(&full, &q);
            assert_eq!(a.iter().map(|x| x.0).collect::<Vec<_>>(), b.iter().map(|x| x.0).collect::<Vec<_>>(), "순서 {:?}", q);
            for (x, y) in a.iter().zip(&b) {
                assert!((x.1 - y.1).abs() < 1e-5, "점수 {:?} {} {}", q, x.1, y.1);
            }
        }
        // 다시 읽어도 같은지(덧붙임 파일 포함)
        let again = Bm25Index::new(Some(p1.clone()));
        assert!(again.ensure_loaded() >= 0.0);
        assert_eq!(again.counts(), Some((5, 2, 2)));
        assert_eq!(rank(&again, &["출장", "규정"]), rank(&seg, &["출장", "규정"]));
        assert_eq!(seg.plan_update(&[0, 2, 3, 5, 6, 7]), Some((vec![7], vec![])));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tf_blob_roundtrip() {
        let t = tf("가 나 가 ▁다");
        assert_eq!(tf_from_blob(&tf_to_blob(&t)), Some(t));
    }
}
