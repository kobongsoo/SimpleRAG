//------------------------------------------------------------------
// 벡터 저장소 (Python index/store.py 의 Qdrant local 대체)
//=> Python 판은 Qdrant local 모드(파일 기반, 사실상 전수 비교)를 쓴다. Rust 에는 서버 없이 쓰는
//   Qdrant 가 없으므로 같은 일을 직접 한다.
//    · 디스크: <데이터 루트>/rag_index/points.sqlite — points(id, vec f32 BLOB, payload JSON)
//    · 메모리: 시작할 때 전부 읽어 정규화한 벡터로 들고 있다 → 코사인 = 내적
//    · 잠금:   rag_index/.lock 을 독점으로 연다 — Qdrant local 처럼 한 프로세스만 연다
//    · 토큰 표: bm25_tf(id, terms) — 청크별 단어 빈도(BM25 를 토큰화 없이 만들려고, 설계서 §4-3)
//    · 벡터 대기: vec 이 길이 0 인 점 — 단계형 인덱싱에서 BM25 로만 찾히고 벡터는 나중에 채운다(§3-5)
//   폴더 한정 검색용 '벡터 한 벌'(VectorMatrix)도 이 메모리 벡터로 바로 한다.
//------------------------------------------------------------------

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Mutex;
use std::time::Instant;

use rusqlite::{params, Connection};
use serde_json::{Map, Value};

use crate::config::cfg;
use crate::pyutil;

pub type Payload = Map<String, Value>;

//------------------------------------------------------------------
// 점 하나
// -필드: vec = 정규화된 벡터 / payload = 본문·출처 등
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Point {
    pub vec: Vec<f32>,
    pub payload: Payload,
}

struct Inner {
    conn: Connection,
    _lock: std::fs::File,
    points: BTreeMap<i64, Point>,
}

//------------------------------------------------------------------
// 저장소 — 적재는 ensure_loaded() 에서(예열 스레드가 미리 부른다)
//------------------------------------------------------------------
pub struct VectorStore {
    pub path: String,
    inner: Mutex<Option<Inner>>,
    load_lock: Mutex<()>,
}

impl Default for VectorStore {
    fn default() -> Self {
        Self::new(None)
    }
}

// 벡터 정규화(0 벡터는 그대로)
fn normalize(v: &mut [f32]) {
    let n = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if n > 1e-12 {
        for x in v.iter_mut() {
            *x /= n;
        }
    }
}

// f32 묶음 ↔ 바이트
fn to_blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}
fn from_blob(b: &[u8]) -> Vec<f32> {
    b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()
}

// 영문 낱말이 단어로 들어 있나 — 앞뒤가 영문 · 숫자가 아니어야 한다(한글이 붙은 "AI기반" 은 맞음)
fn contains_word(hay: &str, w: &str) -> bool {
    let b = hay.as_bytes();
    hay.match_indices(w).any(|(i, _)| {
        let before = i == 0 || !b[i - 1].is_ascii_alphanumeric();
        let after = i + w.len() >= b.len() || !b[i + w.len()].is_ascii_alphanumeric();
        before && after
    })
}

// payload 의 문자열 값
pub fn pl_str<'a>(p: &'a Payload, k: &str) -> &'a str {
    p.get(k).and_then(|v| v.as_str()).unwrap_or("")
}

impl VectorStore {
    pub fn new(path: Option<String>) -> VectorStore {
        VectorStore {
            path: path.unwrap_or_else(|| cfg().index_dir.clone()),
            inner: Mutex::new(None),
            load_lock: Mutex::new(()),
        }
    }

    pub fn is_ready(&self) -> bool {
        self.inner.lock().unwrap().is_some()
    }

    //------------------------------------------------------------------
    // 적재 보장 — 걸린 ms
    //    1) 폴더를 만들고 잠금 파일을 독점으로 연다(이미 열려 있으면 오류)
    //    2) sqlite 를 열어 표를 만들고 모든 점을 메모리로 읽는다
    // -out: error = 다른 프로세스가 쓰는 중이면 Err(RuntimeError …)
    //------------------------------------------------------------------
    pub fn ensure_loaded(&self) -> Result<f64, String> {
        if self.is_ready() {
            return Ok(0.0);
        }
        let _g = self.load_lock.lock().unwrap();
        if self.is_ready() {
            return Ok(0.0);
        }
        let t0 = Instant::now();
        std::fs::create_dir_all(&self.path).map_err(|e| e.to_string())?;
        let lock = open_exclusive(&pyutil::join(&self.path, ".lock")).map_err(|_| {
            format!("RuntimeError: Storage folder {} is already accessed by another instance of SimpleRAG \
                     (인덱스는 한 프로세스만 열 수 있습니다 — 실행 중인 RAGSearchBox·chat 워커를 먼저 끄세요)", self.path)
        })?;
        let conn = Connection::open(pyutil::join(&self.path, "points.sqlite")).map_err(|e| e.to_string())?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS points (id INTEGER PRIMARY KEY, vec BLOB NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bm25_tf (id INTEGER PRIMARY KEY, terms BLOB NOT NULL);")
            .map_err(|e| e.to_string())?;
        let mut points = BTreeMap::new();
        {
            let mut st = conn.prepare("SELECT id, vec, payload FROM points").map_err(|e| e.to_string())?;
            let rows = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?, r.get::<_, String>(2)?)))
                .map_err(|e| e.to_string())?;
            for row in rows.flatten() {
                let mut v = from_blob(&row.1);
                normalize(&mut v);
                let payload = serde_json::from_str::<Payload>(&row.2).unwrap_or_default();
                points.insert(row.0, Point { vec: v, payload });
            }
        }
        *self.inner.lock().unwrap() = Some(Inner { conn, _lock: lock, points });
        Ok(t0.elapsed().as_secs_f64() * 1000.0)
    }

    // 잠금 안에서 일하기
    fn with<R>(&self, f: impl FnOnce(&mut Inner) -> Result<R, String>) -> Result<R, String> {
        self.ensure_loaded()?;
        let mut g = self.inner.lock().unwrap();
        f(g.as_mut().unwrap())
    }

    //------------------------------------------------------------------
    // 재구축 전 벡터 꺼내기 (벡터 재사용 재구축)
    //=> 전체 비우기 직전에 메모리의 점에서 '청크 지문 → 벡터' 표를 만든다. 재구축 중 새 청크의 글이
    //   옛 청크와 같으면 이 벡터를 그대로 써서 다시 임베딩하지 않는다.
    //    1) 점마다 key(payload) 로 지문을 구한다(같은 지문은 처음 것 하나만 — 같은 글이면 벡터도 같다)
    //    2) 벡터가 빈 점(벡터 대기)은 넣지 않는다 — 다시 쓸 것이 없다
    //    3) 모델 확인용 견본 (글, 벡터) 를 고르게 최대 n_sample 개 함께 돌려준다
    //   메모리의 벡터를 옮겨 오므로(복사 아님) 이 뒤에는 반드시 recreate() 로 비워야 한다.
    //
    // -in: key      = payload → 청크 지문
    // -in: n_sample = 견본 수
    //
    // -out: (지문 → 벡터, 견본 [(글, 벡터)])
    // -out: error = 저장소 적재 실패면 Err
    //------------------------------------------------------------------
    pub fn take_vectors(&self, key: impl Fn(&Payload) -> String, n_sample: usize)
                        -> Result<(HashMap<String, Vec<f32>>, Vec<(String, Vec<f32>)>), String> {
        self.with(|inn| {
            let n = inn.points.len();
            // 견본은 전체에서 고르게 — 앞쪽 문서 하나에 몰리지 않게
            let step = (n / n_sample.max(1)).max(1);
            let mut pool: HashMap<String, Vec<f32>> = HashMap::with_capacity(n);
            let mut samples = vec![];
            for (i, p) in inn.points.values_mut().enumerate() {
                if p.vec.is_empty() {
                    continue;
                }
                if i % step == 0 && samples.len() < n_sample {
                    samples.push((pl_str(&p.payload, "text").to_string(), p.vec.clone()));
                }
                let k = key(&p.payload);
                if !pool.contains_key(&k) {
                    pool.insert(k, std::mem::take(&mut p.vec));
                }
            }
            Ok((pool, samples))
        })
    }

    //------------------------------------------------------------------
    // 전체 비우기(--rebuild) — 표를 비우고 남은 점이 0 인지 확인
    //------------------------------------------------------------------
    pub fn recreate(&self) -> Result<(), String> {
        self.with(|inn| {
            inn.conn.execute("DELETE FROM points", []).map_err(|e| e.to_string())?;
            // id 를 0 부터 다시 쓰므로 옛 토큰 표도 비운다
            inn.conn.execute("DELETE FROM bm25_tf", []).map_err(|e| e.to_string())?;
            inn.points.clear();
            let left: i64 = inn.conn.query_row("SELECT COUNT(*) FROM points", [], |r| r.get(0)).map_err(|e| e.to_string())?;
            if left != 0 {
                return Err(format!("RuntimeError: 컬렉션을 비우지 못했습니다(점 {}개 남음): {}", pyutil::comma(left), self.path));
            }
            Ok(())
        })
    }

    //------------------------------------------------------------------
    // 점 넣기(같은 id 는 바꾼다)
    //------------------------------------------------------------------
    pub fn upsert(&self, ids: &[i64], vectors: &[Vec<f32>], payloads: &[Payload]) -> Result<(), String> {
        self.with(|inn| {
            let tx = inn.conn.transaction().map_err(|e| e.to_string())?;
            {
                let mut st = tx.prepare("INSERT OR REPLACE INTO points (id, vec, payload) VALUES (?1, ?2, ?3)").map_err(|e| e.to_string())?;
                for ((id, v), p) in ids.iter().zip(vectors).zip(payloads) {
                    st.execute(params![id, to_blob(v), Value::Object(p.clone()).to_string()]).map_err(|e| e.to_string())?;
                }
            }
            tx.commit().map_err(|e| e.to_string())?;
            for ((id, v), p) in ids.iter().zip(vectors).zip(payloads) {
                let mut vv = v.clone();
                normalize(&mut vv);
                inn.points.insert(*id, Point { vec: vv, payload: p.clone() });
            }
            Ok(())
        })
    }

    // id 목록 지우기(디스크·메모리)
    fn delete_where(inn: &mut Inner, ids: &[i64]) -> Result<(), String> {
        if ids.is_empty() {
            return Ok(());
        }
        let tx = inn.conn.transaction().map_err(|e| e.to_string())?;
        {
            let mut st = tx.prepare("DELETE FROM points WHERE id = ?1").map_err(|e| e.to_string())?;
            // 청크를 지우면 그 청크의 토큰 표도 같이 지운다
            let mut st2 = tx.prepare("DELETE FROM bm25_tf WHERE id = ?1").map_err(|e| e.to_string())?;
            for id in ids {
                st.execute(params![id]).map_err(|e| e.to_string())?;
                st2.execute(params![id]).map_err(|e| e.to_string())?;
            }
        }
        tx.commit().map_err(|e| e.to_string())?;
        for id in ids {
            inn.points.remove(id);
        }
        Ok(())
    }

    //------------------------------------------------------------------
    // 문서 단위 삭제
    //------------------------------------------------------------------
    pub fn delete_doc(&self, doc_path: &str) -> Result<(), String> {
        self.with(|inn| {
            let ids: Vec<i64> = inn.points.iter().filter(|(_, p)| pl_str(&p.payload, "doc_path") == doc_path).map(|(i, _)| *i).collect();
            Self::delete_where(inn, &ids)
        })
    }

    //------------------------------------------------------------------
    // 문서의 점 가운데 keep_ids 만 남기고 지우기
    //------------------------------------------------------------------
    pub fn delete_doc_except(&self, doc_path: &str, keep_ids: &[i64]) -> Result<(), String> {
        let keep: HashSet<i64> = keep_ids.iter().copied().collect();
        self.with(|inn| {
            let ids: Vec<i64> = inn.points.iter()
                .filter(|(i, p)| pl_str(&p.payload, "doc_path") == doc_path && !keep.contains(i))
                .map(|(i, _)| *i).collect();
            Self::delete_where(inn, &ids)
        })
    }

    //------------------------------------------------------------------
    // 점들의 payload 일부 덮어쓰기(문서 이동)
    //------------------------------------------------------------------
    pub fn set_payload_ids(&self, ids: &[i64], payload: &Payload) -> Result<(), String> {
        if ids.is_empty() {
            return Ok(());
        }
        self.with(|inn| {
            let tx = inn.conn.transaction().map_err(|e| e.to_string())?;
            {
                let mut st = tx.prepare("UPDATE points SET payload = ?2 WHERE id = ?1").map_err(|e| e.to_string())?;
                for id in ids {
                    if let Some(p) = inn.points.get_mut(id) {
                        for (k, v) in payload {
                            p.payload.insert(k.clone(), v.clone());
                        }
                        st.execute(params![id, Value::Object(p.payload.clone()).to_string()]).map_err(|e| e.to_string())?;
                    }
                }
            }
            tx.commit().map_err(|e| e.to_string())
        })
    }

    //------------------------------------------------------------------
    // 점들의 벡터·payload 읽기(문서 복사본) — 하나라도 없으면 None
    //------------------------------------------------------------------
    pub fn points_of(&self, ids: &[i64]) -> Result<Option<(Vec<Vec<f32>>, Vec<Payload>)>, String> {
        self.with(|inn| {
            let mut vs = vec![];
            let mut ps = vec![];
            for id in ids {
                match inn.points.get(id) {
                    Some(p) => {
                        vs.push(p.vec.clone());
                        ps.push(p.payload.clone());
                    }
                    None => return Ok(None),
                }
            }
            Ok(Some((vs, ps)))
        })
    }

    pub fn delete_ids(&self, ids: &[i64]) -> Result<(), String> {
        self.with(|inn| Self::delete_where(inn, ids))
    }

    //------------------------------------------------------------------
    // 벡터 검색 — (id, 점수, payload) 점수 내림차순
    // -in: allow = Some(허용 id 집합) 이면 그 안에서만(폴더 한정)
    //------------------------------------------------------------------
    pub fn search(&self, qv: &[f32], limit: usize, allow: Option<&HashSet<i64>>) -> Result<Vec<(i64, f32, Payload)>, String> {
        let mut q = qv.to_vec();
        normalize(&mut q);
        self.with(|inn| {
            // 벡터 대기 점(길이 0)은 건너뛴다 — 0점으로 줄 세우지 않는다(BM25 로만 찾힌다)
            let mut scored: Vec<(f32, i64)> = inn.points.iter()
                .filter(|(i, p)| !p.vec.is_empty() && allow.map(|a| a.contains(i)).unwrap_or(true))
                .map(|(i, p)| (p.vec.iter().zip(&q).map(|(a, b)| a * b).sum::<f32>(), *i)).collect();
            scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1)));
            scored.truncate(limit);
            Ok(scored.into_iter().map(|(s, i)| (i, s, inn.points[&i].payload.clone())).collect())
        })
    }

    //------------------------------------------------------------------
    // id 들의 payload (없는 id 는 빠진다)
    //------------------------------------------------------------------
    pub fn payloads(&self, ids: &[i64]) -> Result<HashMap<i64, Payload>, String> {
        self.with(|inn| Ok(ids.iter().filter_map(|i| inn.points.get(i).map(|p| (*i, p.payload.clone()))).collect()))
    }

    //------------------------------------------------------------------
    // 폴더 범위 — (그 폴더 아래 문서 수, 허용 id 집합)
    //=> 경로는 normcase 로 비교하고 "폴더\" 로 시작하는 문서만 센다(하위 포함).
    //------------------------------------------------------------------
    pub fn folder_scope(&self, folder: &str) -> Result<(usize, HashSet<i64>), String> {
        let f = norm_dir(folder);
        self.with(|inn| {
            let mut docs = HashSet::new();
            let mut ids = HashSet::new();
            for (i, p) in &inn.points {
                let d = pl_str(&p.payload, "doc_path");
                if under(&pyutil::normcase(d), &f) {
                    docs.insert(d.to_string());
                    ids.insert(*i);
                }
            }
            Ok((docs.len(), ids))
        })
    }

    //------------------------------------------------------------------
    // 문서 집합 범위 — (그 문서들 중 인덱스에 있는 문서 수, 허용 id 집합)
    //=> 메타데이터 조건에 맞는 문서만 찾게 한다(메타데이터 검색 설계서 §13-1 — 폴더 한정과 같은 방식).
    //   폴더를 주면 그 폴더 아래로 더 좁힌다. 경로는 normcase 로 비교한다.
    //
    // -in: docs = 문서 경로 집합 / folder = 폴더(None 이면 전체)
    // -out: (문서 수, 허용 id 집합)
    //------------------------------------------------------------------
    pub fn docs_scope(&self, docs: &HashSet<String>, folder: Option<&str>) -> Result<(usize, HashSet<i64>), String> {
        let want: HashSet<String> = docs.iter().map(|d| pyutil::normcase(d)).collect();
        let f = folder.map(norm_dir);
        self.with(|inn| {
            let mut seen = HashSet::new();
            let mut ids = HashSet::new();
            for (i, p) in &inn.points {
                let d = pyutil::normcase(pl_str(&p.payload, "doc_path"));
                if want.contains(&d) && f.as_ref().map_or(true, |f| under(&d, f)) {
                    seen.insert(d);
                    ids.insert(*i);
                }
            }
            Ok((seen.len(), ids))
        })
    }

    //------------------------------------------------------------------
    // 한 절의 청크 payload (chunk_idx 순)
    //------------------------------------------------------------------
    pub fn section_chunks(&self, doc_path: &str, section_id: i64) -> Result<Vec<Payload>, String> {
        self.with(|inn| {
            let mut v: Vec<Payload> = inn.points.values()
                .filter(|p| pl_str(&p.payload, "doc_path") == doc_path && p.payload.get("section_id").and_then(|x| x.as_i64()) == Some(section_id))
                .map(|p| p.payload.clone()).collect();
            v.sort_by_key(|p| p.get("chunk_idx").and_then(|x| x.as_i64()).unwrap_or(0));
            Ok(v)
        })
    }

    //------------------------------------------------------------------
    // 모든 점의 (id, 본문, doc_path) — id 순 (BM25 재구축·고아 청소)
    //------------------------------------------------------------------
    pub fn scroll_all(&self) -> Result<Vec<(i64, String, String)>, String> {
        self.with(|inn| Ok(inn.points.iter().map(|(i, p)| (*i, pl_str(&p.payload, "text").to_string(), pl_str(&p.payload, "doc_path").to_string())).collect()))
    }

    //------------------------------------------------------------------
    // 모든 점의 (id, doc_path) — id 순 (고아 청소 — 본문은 읽지 않는다)
    //------------------------------------------------------------------
    pub fn doc_paths(&self) -> Result<Vec<(i64, String)>, String> {
        self.with(|inn| Ok(inn.points.iter().map(|(i, p)| (*i, pl_str(&p.payload, "doc_path").to_string())).collect()))
    }

    //------------------------------------------------------------------
    // 모든 점 id (id 순) — BM25 를 고칠 때 색인과 맞대어 본다
    //------------------------------------------------------------------
    pub fn ids(&self) -> Result<Vec<i64>, String> {
        self.with(|inn| Ok(inn.points.keys().copied().collect()))
    }

    //------------------------------------------------------------------
    // 점들의 본문 (없는 id 는 빈 글)
    //------------------------------------------------------------------
    //------------------------------------------------------------------
    // 본문 글자 훑기 (모두 찾기 설계서 §7)
    //=> 메모리에 올린 청크 본문에서 낱말이 글자 그대로 든 청크를 찾는다.
    //    1) 앞 접두 "[문서 > 절]" 줄은 빼고 본다 — 문서 이름만 맞는 청크가 모두 걸리지 않게
    //    2) 영문이 섞인 낱말이 있으면 대소문자를 가리지 않는다
    //
    // -in: words = 찾을 낱말(앞 32개까지) / allow = 이 id 들 안에서만(None = 전부)
    //
    // -out: [(청크 id, 맞은 낱말 비트, 문서 경로)]
    // -out: error = 적재 실패면 Err
    //------------------------------------------------------------------
    pub fn scan_words(&self, words: &[String], allow: Option<&HashSet<i64>>) -> Result<Vec<(i64, u32, String)>, String> {
        let ws: Vec<String> = words.iter().take(32).map(|w| w.to_ascii_lowercase()).collect();
        let fold = ws.iter().any(|w| w.bytes().any(|b| b.is_ascii_alphabetic()));
        // 영문 · 숫자로만 된 낱말은 단어 경계로 — "ai" 가 "maintain" 안에서 걸리지 않게
        let ascii: Vec<bool> = ws.iter().map(|w| w.bytes().all(|b| b.is_ascii_alphanumeric())).collect();
        self.with(|inn| {
            let mut out = vec![];
            for (i, p) in &inn.points {
                if allow.is_some_and(|a| !a.contains(i)) {
                    continue;
                }
                let text = pl_str(&p.payload, "text");
                let body = if text.starts_with('[') { text.find("]\n").map_or(text, |k| &text[k + 2..]) } else { text };
                let low;
                let hay: &str = if fold {
                    low = body.to_ascii_lowercase();
                    &low
                } else {
                    body
                };
                let mut mask = 0u32;
                for (k, w) in ws.iter().enumerate() {
                    let hit = if ascii[k] { contains_word(hay, w) } else { hay.contains(w.as_str()) };
                    if hit {
                        mask |= 1 << k;
                    }
                }
                if mask != 0 {
                    out.push((*i, mask, pl_str(&p.payload, "doc_path").to_string()));
                }
            }
            Ok(out)
        })
    }

    pub fn texts_of(&self, ids: &[i64]) -> Result<Vec<String>, String> {
        self.with(|inn| Ok(ids.iter().map(|i| inn.points.get(i).map(|p| pl_str(&p.payload, "text").to_string()).unwrap_or_default()).collect()))
    }

    //------------------------------------------------------------------
    // 청크별 단어 빈도 넣기 (설계서 §4-3 단계 B)
    //=> 청크를 넣을 때 그 청크의 토큰화 결과를 함께 남겨, BM25 를 다시 만들 때 토큰화를 건너뛴다.
    //
    // -in: ids = 점 id / blobs = 같은 순서의 bm25::tf_to_blob 결과
    //
    // -out: 없음
    // -out: error = sqlite 쓰기 실패면 Err
    //------------------------------------------------------------------
    pub fn put_tf(&self, ids: &[i64], blobs: &[Vec<u8>]) -> Result<(), String> {
        if ids.is_empty() {
            return Ok(());
        }
        self.with(|inn| {
            let tx = inn.conn.transaction().map_err(|e| e.to_string())?;
            {
                let mut st = tx.prepare("INSERT OR REPLACE INTO bm25_tf (id, terms) VALUES (?1, ?2)").map_err(|e| e.to_string())?;
                for (id, b) in ids.iter().zip(blobs) {
                    st.execute(params![id, b]).map_err(|e| e.to_string())?;
                }
            }
            tx.commit().map_err(|e| e.to_string())
        })
    }

    //------------------------------------------------------------------
    // 청크별 단어 빈도 읽기 — {id: 바이트}, 표에 없는 id 는 빠진다
    //
    // -in: ids = Some(점 id 목록) 이면 그것만, None 이면 표 전체
    //
    // -out: {id: tf 바이트}
    // -out: error = sqlite 읽기 실패면 Err
    //------------------------------------------------------------------
    pub fn tf_of(&self, ids: Option<&[i64]>) -> Result<HashMap<i64, Vec<u8>>, String> {
        self.with(|inn| {
            let mut out = HashMap::new();
            match ids {
                None => {
                    let mut st = inn.conn.prepare("SELECT id, terms FROM bm25_tf").map_err(|e| e.to_string())?;
                    let rows = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?))).map_err(|e| e.to_string())?;
                    for row in rows.flatten() {
                        out.insert(row.0, row.1);
                    }
                }
                Some(ids) => {
                    let mut st = inn.conn.prepare("SELECT terms FROM bm25_tf WHERE id = ?1").map_err(|e| e.to_string())?;
                    for id in ids {
                        if let Ok(b) = st.query_row(params![id], |r| r.get::<_, Vec<u8>>(0)) {
                            out.insert(*id, b);
                        }
                    }
                }
            }
            Ok(out)
        })
    }

    //------------------------------------------------------------------
    // 토큰 표 정리 — 저장소에 없는 점의 줄을 지운다(고아 청소 뒤 · 옛 찌꺼기)
    //
    // -out: 지운 줄 수
    // -out: error = sqlite 쓰기 실패면 Err
    //------------------------------------------------------------------
    pub fn prune_tf(&self) -> Result<usize, String> {
        self.with(|inn| {
            inn.conn.execute("DELETE FROM bm25_tf WHERE id NOT IN (SELECT id FROM points)", []).map_err(|e| e.to_string())
        })
    }

    //------------------------------------------------------------------
    // 벡터 대기 청크 수 (단계형 인덱싱 §3-5)
    //------------------------------------------------------------------
    pub fn pending_count(&self) -> Result<usize, String> {
        self.with(|inn| Ok(inn.points.values().filter(|p| p.vec.is_empty()).count()))
    }

    //------------------------------------------------------------------
    // 벡터 대기 청크 앞에서부터 — [(id, 본문, doc_path)] id 순 (먼저 넣은 문서부터 채운다)
    //
    // -in: limit = 최대 개수
    //------------------------------------------------------------------
    pub fn pending_batch(&self, limit: usize) -> Result<Vec<(i64, String, String)>, String> {
        self.with(|inn| Ok(inn.points.iter().filter(|(_, p)| p.vec.is_empty()).take(limit)
            .map(|(i, p)| (*i, pl_str(&p.payload, "text").to_string(), pl_str(&p.payload, "doc_path").to_string())).collect()))
    }

    //------------------------------------------------------------------
    // 점들의 벡터만 바꾸기 (대기 청크 채우기) — payload 는 그대로
    //=> 채우는 사이에 문서가 지워졌으면(점이 없으면) 그 점은 건너뛴다.
    //
    // -in: ids = 점 id / vectors = 같은 순서의 벡터
    //
    // -out: 실제로 바꾼 점 수
    // -out: error = sqlite 쓰기 실패면 Err
    //------------------------------------------------------------------
    pub fn set_vectors(&self, ids: &[i64], vectors: &[Vec<f32>]) -> Result<usize, String> {
        self.with(|inn| {
            let tx = inn.conn.transaction().map_err(|e| e.to_string())?;
            let mut n = 0;
            {
                let mut st = tx.prepare("UPDATE points SET vec = ?2 WHERE id = ?1").map_err(|e| e.to_string())?;
                for (id, v) in ids.iter().zip(vectors) {
                    if inn.points.contains_key(id) {
                        st.execute(params![id, to_blob(v)]).map_err(|e| e.to_string())?;
                        n += 1;
                    }
                }
            }
            tx.commit().map_err(|e| e.to_string())?;
            for (id, v) in ids.iter().zip(vectors) {
                if let Some(p) = inn.points.get_mut(id) {
                    let mut vv = v.clone();
                    normalize(&mut vv);
                    p.vec = vv;
                }
            }
            Ok(n)
        })
    }

    //------------------------------------------------------------------
    // 통계 — (점 수, 디스크 MB)
    //------------------------------------------------------------------
    pub fn stats(&self) -> Result<(usize, f64), String> {
        let n = self.with(|inn| Ok(inn.points.len()))?;
        let mut size = 0u64;
        if let Ok(rd) = std::fs::read_dir(&self.path) {
            for e in rd.flatten() {
                size += e.metadata().map(|m| m.len()).unwrap_or(0);
            }
        }
        Ok((n, pyutil::round_to(size as f64 / 1024.0 / 1024.0, 1)))
    }

    //------------------------------------------------------------------
    // 닫기 — 잠금 파일을 놓는다
    //------------------------------------------------------------------
    pub fn close(&self) {
        *self.inner.lock().unwrap() = None;
    }
}

//------------------------------------------------------------------
// 파일을 독점(공유 없음)으로 열기 — 다른 프로세스가 열고 있으면 실패
//------------------------------------------------------------------
pub fn open_exclusive(path: &str) -> std::io::Result<std::fs::File> {
    use std::os::windows::fs::OpenOptionsExt;
    std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).share_mode(0).open(path)
}

// 폴더 비교용 표기(끝 구분자 없음)
pub fn norm_dir(path: &str) -> String {
    pyutil::rstrip_chars(&pyutil::normcase(&pyutil::abspath(path)), "\\/").to_string()
}

// doc 이 폴더 아래(하위 포함)인가
pub fn under(doc_norm: &str, folder_norm: &str) -> bool {
    doc_norm.starts_with(&format!("{}\\", folder_norm))
}
