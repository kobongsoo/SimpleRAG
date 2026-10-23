//------------------------------------------------------------------
// 벡터 저장소 (Python index/store.py 의 Qdrant local 대체)
//=> Python 판은 Qdrant local 모드(파일 기반, 사실상 전수 비교)를 쓴다. Rust 에는 서버 없이 쓰는
//   Qdrant 가 없으므로 같은 일을 직접 한다.
//    · 디스크: <데이터 루트>/rag_index/points.sqlite — points(id, vec f32 BLOB, payload JSON)
//    · 메모리: 시작할 때 전부 읽어 정규화한 벡터로 들고 있다 → 코사인 = 내적
//    · 잠금:   rag_index/.lock 을 독점으로 연다 — Qdrant local 처럼 한 프로세스만 연다
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
            CREATE TABLE IF NOT EXISTS points (id INTEGER PRIMARY KEY, vec BLOB NOT NULL, payload TEXT NOT NULL);")
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
    // 전체 비우기(--rebuild) — 표를 비우고 남은 점이 0 인지 확인
    //------------------------------------------------------------------
    pub fn recreate(&self) -> Result<(), String> {
        self.with(|inn| {
            inn.conn.execute("DELETE FROM points", []).map_err(|e| e.to_string())?;
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
            for id in ids {
                st.execute(params![id]).map_err(|e| e.to_string())?;
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
            let mut scored: Vec<(f32, i64)> = inn.points.iter()
                .filter(|(i, _)| allow.map(|a| a.contains(i)).unwrap_or(true))
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
