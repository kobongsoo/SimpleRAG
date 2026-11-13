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
// 문서 단위 열쇠 — 같은 문서의 청크들이 한 벌을 나눠 가진다(Arc)
//=> 경로·내용 해시는 청크마다 똑같은 값이 되풀이된다. 한 벌만 두면 메모리가 줄고,
//   normcase(경로)를 미리 구해 두면 폴더 한정 · 비슷한 문서가 점마다 다시 계산하지 않는다.
// -필드: path = doc_path 원래 글자 / norm = pyutil::normcase(path) / content_hash = 파일 내용 sha1
//------------------------------------------------------------------
#[derive(Debug)]
pub struct DocKey {
    pub path: Box<str>,
    pub norm: Box<str>,
    pub content_hash: Box<str>,
}

//------------------------------------------------------------------
// 점 하나 (메모리 절감 설계서 A안 — payload 원문 보관)
//=> 예전에는 payload 를 serde_json Map 으로 풀어 들고 있었다. 청크 28만 × 필드 14개라
//   그릇 부담이 내용의 4.5배(디스크 242MB → 램 약 1.1GB)였다.
//   이제 디스크에 쓴 JSON 글자(raw)를 그대로 들고 있다가 돌려줄 때만 풀고(1개 약 15µs),
//   전체를 훑는 일(모두 찾기 · 폴더 한정 · 비슷한 문서 · BM25 재구축)이 쓰는 필드만 따로 꺼내 둔다.
//   raw 를 그대로 풀므로 돌려주는 Map 은 예전과 글자 하나 다르지 않다(필드 순서 · 숫자 표기 포함).
// -필드: vec = 정규화된 벡터(빈 벡터 = 벡터 대기) — f16 또는 f32 (B안)
// -필드: raw = payload JSON 글자 / text = 본문 / doc = 문서 열쇠(공유)
// -필드: chunk_idx = payload chunk_idx(없으면 0 — 정렬 기본값과 같게) / section_id = payload section_id
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Point {
    vec: Vector,
    raw: Box<str>,
    text: Box<str>,
    doc: std::sync::Arc<DocKey>,
    chunk_idx: i64,
    section_id: Option<i64>,
}

//------------------------------------------------------------------
// 메모리 벡터 한 개 — f32 또는 반 정밀도 f16 (메모리 절감 설계서 B안)
//=> 디스크는 늘 f32. 메모리만 f16 으로 들면 벡터 몫이 절반(약 434 → 217MB)이 된다.
//   어느 쪽인지는 저장소를 열 때 index.vector_f16(기본 켬)으로 정한다.
//   메모리 값을 디스크로 다시 쓰는 길(문서 복사 · 재구축 재사용)은 여기서 꺼내지 않고 sqlite 의 f32 를 읽는다
//   — f16 으로 깎인 값이 디스크에 쌓이지 않게(points_of · take_vectors).
//------------------------------------------------------------------
#[derive(Debug, Clone)]
enum Vector {
    F32(Vec<f32>),
    F16(Vec<u16>),
}

impl Default for Vector {
    fn default() -> Self {
        Vector::F32(vec![])
    }
}

impl Vector {
    // 정규화된 f32 벡터를 저장 방식에 맞게 담기
    fn new(f16: bool, v: Vec<f32>) -> Vector {
        if f16 { Vector::F16(crate::vecf16::encode(&v)) } else { Vector::F32(v) }
    }

    // 벡터 대기 점(길이 0)인가
    fn is_empty(&self) -> bool {
        match self {
            Vector::F32(v) => v.is_empty(),
            Vector::F16(v) => v.is_empty(),
        }
    }

    // 정규화된 질의 q 와의 내적(f16 이면 칸을 풀면서 f32 로 더한다)
    fn dot(&self, q: &[f32]) -> f32 {
        match self {
            Vector::F32(v) => dot(v, q),
            Vector::F16(v) => crate::vecf16::dot(v, q),
        }
    }

    // f32 로 꺼내기 — 질의로만 쓰는 곳(비슷한 문서의 기준 문서)
    fn to_f32(&self) -> Vec<f32> {
        match self {
            Vector::F32(v) => v.clone(),
            Vector::F16(v) => crate::vecf16::decode(v),
        }
    }
}

// 문서 열쇠 공유 표 — "경로\0내용해시" → 열쇠
type DocTable = HashMap<String, std::sync::Arc<DocKey>>;

impl Point {
    //------------------------------------------------------------------
    // payload JSON 글자로 점 만들기 — 적재 · 넣기 · 고치기가 모두 이 길로 만든다
    //=> raw 와 훑기 필드를 늘 함께 만들어 둘이 어긋나지 않게 한다.
    //    1) JSON 을 한 번 풀어 훑기 필드(본문 · 경로 · 해시 · 청크 번호 · 절 번호)를 꺼낸다
    //    2) 같은 (경로, 해시) 문서 열쇠가 표에 있으면 그것을 나눠 쓰고, 없으면 새로 넣는다
    //    3) 푼 Map 은 버리고 글자만 남긴다
    //
    // -in: vec = 정규화된 벡터 / raw = payload JSON 글자 / docs = 문서 열쇠 공유 표
    //
    // -out: 점
    // -out: error = 없음 (JSON 이 깨졌으면 빈 payload 로 본다 — 예전 unwrap_or_default 와 같다)
    //------------------------------------------------------------------
    fn from_raw(vec: Vector, raw: String, docs: &mut DocTable) -> Point {
        // 깨진 JSON 은 예전처럼 빈 payload — 돌려줄 때도 빈 Map 이 되게 "{}" 로 둔다
        let (m, raw) = match serde_json::from_str::<Payload>(&raw) {
            Ok(m) => (m, raw),
            Err(_) => (Payload::new(), "{}".to_string()),
        };
        let path = pl_str(&m, "doc_path");
        let hash = pl_str(&m, "content_hash");
        let doc = docs.entry(format!("{}\0{}", path, hash)).or_insert_with(|| std::sync::Arc::new(DocKey {
            path: path.into(),
            norm: pyutil::normcase(path).into(),
            content_hash: hash.into(),
        })).clone();
        Point {
            vec,
            text: pl_str(&m, "text").into(),
            doc,
            chunk_idx: m.get("chunk_idx").and_then(|x| x.as_i64()).unwrap_or(0),
            section_id: m.get("section_id").and_then(|x| x.as_i64()),
            raw: raw.into_boxed_str(),
        }
    }

    // payload Map 으로 점 만들기 — 디스크에 쓰는 글자와 같은 글자를 raw 로
    fn from_payload(vec: Vector, p: &Payload, docs: &mut DocTable) -> Point {
        Point::from_raw(vec, Value::Object(p.clone()).to_string(), docs)
    }

    //------------------------------------------------------------------
    // payload 풀기 — 돌려줄 때만 부른다(1개 약 15µs)
    // -out: payload Map / error = 없음 (from_raw 가 늘 풀리는 글자만 남긴다)
    //------------------------------------------------------------------
    pub fn payload(&self) -> Payload {
        serde_json::from_str(&self.raw).unwrap_or_default()
    }
}

struct Inner {
    conn: Connection,
    _lock: std::fs::File,
    points: BTreeMap<i64, Point>,
    // 문서 열쇠 공유 표 — 점을 지울 때 아무도 안 쓰는 열쇠는 걷어 낸다
    docs: DocTable,
    // 메모리 벡터를 f16 으로 드나(열 때 index.vector_f16 으로 정한다)
    f16: bool,
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

//------------------------------------------------------------------
// 문서별 최고 점수 훑기 결과 (doc_max_scan)
// -필드: docs   = 문서 경로(훑은 순서)
// -필드: best   = 문서마다 질의 청크별 (최고 내적, 그 청크 id) — 벡터가 하나도 없으면 (-1, -1)
// -필드: same   = 문서마다 기준 문서와 본문이 그대로 같은 본문 열쇠 집합
// -필드: chunks = 문서마다 청크 수 / first = 문서마다 (가장 작은 id, 그 payload) — 이름 · 해시 표시용
//------------------------------------------------------------------
#[derive(Debug, Default, Clone)]
pub struct DocScan {
    pub docs: Vec<String>,
    pub best: Vec<Vec<(f32, i64)>>,
    pub same: Vec<HashSet<u64>>,
    pub chunks: Vec<usize>,
    pub first: Vec<(i64, Payload)>,
}

//------------------------------------------------------------------
// 내적 — 8칸씩 나눠 더한다
//=> 한 칸씩 더하면 컴파일러가 실수 덧셈 순서를 지키느라 SIMD 로 묶지 못한다. 칸을 나누면 묶여 몇 배 빠르다.
//
// -in: a · b = 같은 길이의 벡터
//
// -out: 내적
// -out: error = 없음 (길이가 다르면 짧은 쪽까지만)
//------------------------------------------------------------------
pub fn dot(a: &[f32], b: &[f32]) -> f32 {
    // AVX·FMA 가 있으면 8칸을 한 명령으로 곱해 더한다(시작 때 한 번 확인한 값)
    #[cfg(target_arch = "x86_64")]
    if *USE_AVX {
        // SAFETY: USE_AVX 가 참이면 이 CPU 에 avx · fma 가 있다
        return unsafe { dot_avx(a, b) };
    }
    dot_plain(a, b)
}

// AVX·FMA 를 쓸까 — CPU 에 있고, SIMPLERAG_NO_AVX=1 로 끄지 않았을 때(비교 측정용 스위치)
#[cfg(target_arch = "x86_64")]
static USE_AVX: once_cell::sync::Lazy<bool> = once_cell::sync::Lazy::new(|| {
    std::env::var("SIMPLERAG_NO_AVX").map(|v| v.trim() != "1").unwrap_or(true)
        && is_x86_feature_detected!("avx")
        && is_x86_feature_detected!("fma")
});

//------------------------------------------------------------------
// 내적 — AVX·FMA 판 (메모리 절감 설계서 §5-2)
//=> 빌드가 AVX 를 켜지 않아(기본 x86-64) dot_plain 은 SSE 4칸만 쓴다. 이 판은 8칸을 FMA 로 곱해 더한다.
//   실측(2026-11-13, 28만 × 384): 질문당 전체 훑기 68.7ms → 28.8ms.
//   더하는 순서가 dot_plain 과 달라 점수 끝자리(1e-7 안팎)가 다를 수 있다.
//
// -in: a · b = 같은 길이의 벡터
//
// -out: 내적
// -out: error = 없음 (길이가 다르면 짧은 쪽까지만)
//------------------------------------------------------------------
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx,fma")]
unsafe fn dot_avx(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;
    let n = a.len().min(b.len());
    let mut acc = _mm256_setzero_ps();
    let mut i = 0;
    while i + 8 <= n {
        acc = _mm256_fmadd_ps(_mm256_loadu_ps(a.as_ptr().add(i)), _mm256_loadu_ps(b.as_ptr().add(i)), acc);
        i += 8;
    }
    let mut t = [0f32; 8];
    _mm256_storeu_ps(t.as_mut_ptr(), acc);
    let mut s: f32 = t.iter().sum();
    // 8 로 나눠떨어지지 않는 꼬리
    while i < n {
        s += a[i] * b[i];
        i += 1;
    }
    s
}

//------------------------------------------------------------------
// 디스크(sqlite)의 벡터 한 개 — 정규화한 f32
//=> 메모리 벡터는 f16 일 수 있어, 디스크로 다시 쓸 벡터(문서 복사 · 재구축 재사용)는 여기서 읽는다.
//   정규화까지 하므로 f32 모드에서는 예전 메모리 값과 똑같다. 벡터 대기 점은 빈 벡터.
//
// -in: st = "SELECT vec FROM points WHERE id = ?1" 문 / id = 점 id
//
// -out: 정규화한 벡터
// -out: error = sqlite 읽기 실패(점이 없음 포함)면 Err
//------------------------------------------------------------------
fn disk_vec(st: &mut rusqlite::Statement, id: i64) -> Result<Vec<f32>, String> {
    let b: Vec<u8> = st.query_row(params![id], |r| r.get(0)).map_err(|e| e.to_string())?;
    let mut v = from_blob(&b);
    normalize(&mut v);
    Ok(v)
}

// 내적 — 명령 없는 판(8칸 묶음, 컴파일러가 SSE 로 묶는다)
fn dot_plain(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len().min(b.len());
    let mut acc = [0f32; 8];
    let mut i = 0;
    while i + 8 <= n {
        for k in 0..8 {
            acc[k] += a[i + k] * b[i + k];
        }
        i += 8;
    }
    let mut s: f32 = acc.iter().sum();
    // 8 로 나눠떨어지지 않는 꼬리
    while i < n {
        s += a[i] * b[i];
        i += 1;
    }
    s
}

//------------------------------------------------------------------
// 점 한 묶음 훑기 (doc_max_scan 의 스레드 하나)
//=> 점마다 문서 칸을 찾아(처음이면 만든다) 질의별 최고 점수와 같은 본문을 갱신한다.
//
// -in: part = (id, 점) 묶음 / qs = 정규화된 질의 벡터 / bodies · body_key = doc_max_scan 과 같음
//
// -out: 이 묶음의 DocScan
// -out: error = 없음
//------------------------------------------------------------------
fn scan_part(part: &[(&i64, &Point)], qs: &[Vec<f32>], bodies: &HashSet<u64>, body_key: fn(&str) -> Option<u64>) -> DocScan {
    let mut out = DocScan::default();
    let mut at: HashMap<&str, usize> = HashMap::new();
    for (id, p) in part {
        let d: &str = &p.doc.path;
        let j = match at.get(d) {
            Some(&j) => j,
            None => {
                at.insert(d, out.docs.len());
                out.docs.push(d.to_string());
                out.best.push(vec![(-1.0, -1); qs.len()]);
                out.same.push(HashSet::new());
                out.chunks.push(0);
                // 대표 payload 는 id 만 적어 두고, 합친 뒤 문서마다 한 번만 푼다(doc_max_scan)
                out.first.push((**id, Payload::new()));
                out.docs.len() - 1
            }
        };
        out.chunks[j] += 1;
        if **id < out.first[j].0 {
            out.first[j].0 = **id;
        }
        // 본문이 기준 문서와 그대로 같은가 — 사본 · 다른 판 판정
        if !bodies.is_empty() {
            if let Some(k) = body_key(&p.text) {
                if bodies.contains(&k) {
                    out.same[j].insert(k);
                }
            }
        }
        // 벡터 대기 점은 내적하지 않는다(BM25 로만 찾히는 점)
        if p.vec.is_empty() {
            continue;
        }
        for (q, qv) in qs.iter().enumerate() {
            let s = p.vec.dot(qv);
            if s > out.best[j][q].0 {
                out.best[j][q] = (s, **id);
            }
        }
    }
    out
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
        let mut docs = DocTable::new();
        let f16 = cfg().vector_f16;
        {
            let mut st = conn.prepare("SELECT id, vec, payload FROM points").map_err(|e| e.to_string())?;
            let rows = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?, r.get::<_, String>(2)?)))
                .map_err(|e| e.to_string())?;
            for row in rows.flatten() {
                let mut v = from_blob(&row.1);
                normalize(&mut v);
                // payload 는 디스크 글자 그대로 들고 훑기 필드만 꺼낸다(A안)
                points.insert(row.0, Point::from_raw(Vector::new(f16, v), row.2, &mut docs));
            }
        }
        *self.inner.lock().unwrap() = Some(Inner { conn, _lock: lock, points, docs, f16 });
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
    //   벡터는 메모리(f16 일 수 있음)가 아니라 sqlite 의 f32 를 읽어 정규화한다 — 재구축을 거칠 때마다
    //   f16 으로 깎인 값이 디스크에 쌓이지 않게(B안). f32 모드에서는 예전 메모리 값과 똑같다.
    //   읽은 점의 메모리 벡터는 비운다 — 이 뒤에는 반드시 recreate() 로 비워야 한다.
    //
    // -in: key      = payload → 청크 지문
    // -in: n_sample = 견본 수
    //
    // -out: (지문 → 벡터, 견본 [(글, 벡터)])
    // -out: error = 저장소 적재 · sqlite 읽기 실패면 Err
    //------------------------------------------------------------------
    pub fn take_vectors(&self, key: impl Fn(&Payload) -> String, n_sample: usize)
                        -> Result<(HashMap<String, Vec<f32>>, Vec<(String, Vec<f32>)>), String> {
        self.with(|inn| {
            let n = inn.points.len();
            // 견본은 전체에서 고르게 — 앞쪽 문서 하나에 몰리지 않게
            let step = (n / n_sample.max(1)).max(1);
            let mut pool: HashMap<String, Vec<f32>> = HashMap::with_capacity(n);
            let mut samples = vec![];
            let mut st = inn.conn.prepare("SELECT vec FROM points WHERE id = ?1").map_err(|e| e.to_string())?;
            for (i, (id, p)) in inn.points.iter_mut().enumerate() {
                if p.vec.is_empty() {
                    continue;
                }
                let need_sample = i % step == 0 && samples.len() < n_sample;
                // 지문 함수는 payload 를 받는다 — 재구축 때 한 번뿐이라 점마다 풀어 넘긴다
                let k = key(&p.payload());
                if need_sample || !pool.contains_key(&k) {
                    let v = disk_vec(&mut st, *id)?;
                    if need_sample {
                        samples.push((p.text.to_string(), v.clone()));
                    }
                    pool.entry(k).or_insert(v);
                }
                p.vec = Vector::default();
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
            inn.docs.clear();
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
                // 디스크에 쓴 것과 같은 글자를 raw 로 — 같은 id 를 바꾸면 옛 점의 문서 열쇠는 아래 청소가 걷는다
                let pt = Point::from_payload(Vector::new(inn.f16, vv), p, &mut inn.docs);
                inn.points.insert(*id, pt);
            }
            Self::prune_docs(inn);
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
        Self::prune_docs(inn);
        Ok(())
    }

    // 아무 점도 안 쓰는 문서 열쇠 걷어 내기(표 자신만 쥐고 있으면 strong_count 가 1)
    fn prune_docs(inn: &mut Inner) {
        inn.docs.retain(|_, d| std::sync::Arc::strong_count(d) > 1);
    }

    //------------------------------------------------------------------
    // 문서 단위 삭제
    //------------------------------------------------------------------
    pub fn delete_doc(&self, doc_path: &str) -> Result<(), String> {
        self.with(|inn| {
            let ids: Vec<i64> = inn.points.iter().filter(|(_, p)| &*p.doc.path == doc_path).map(|(i, _)| *i).collect();
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
                .filter(|(i, p)| &*p.doc.path == doc_path && !keep.contains(i))
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
                        // 풀어서 덮어쓰고 다시 점으로 — raw 와 훑기 필드(경로 등)를 함께 새로 만든다
                        let mut pl = p.payload();
                        for (k, v) in payload {
                            pl.insert(k.clone(), v.clone());
                        }
                        let vec = std::mem::take(&mut p.vec);
                        *p = Point::from_payload(vec, &pl, &mut inn.docs);
                        st.execute(params![id, &*p.raw]).map_err(|e| e.to_string())?;
                    }
                }
            }
            tx.commit().map_err(|e| e.to_string())?;
            Self::prune_docs(inn);
            Ok(())
        })
    }

    //------------------------------------------------------------------
    // 점들의 벡터·payload 읽기(문서 복사본) — 하나라도 없으면 None
    //=> 돌려준 벡터는 새 점으로 디스크에 다시 쓰인다. 그래서 메모리(f16 일 수 있음)가 아니라
    //   sqlite 의 f32 를 읽는다(disk_vec — 정규화까지 해 f32 모드에서는 예전과 같다).
    //------------------------------------------------------------------
    pub fn points_of(&self, ids: &[i64]) -> Result<Option<(Vec<Vec<f32>>, Vec<Payload>)>, String> {
        self.with(|inn| {
            let mut vs = vec![];
            let mut ps = vec![];
            let mut st = inn.conn.prepare("SELECT vec FROM points WHERE id = ?1").map_err(|e| e.to_string())?;
            for id in ids {
                match inn.points.get(id) {
                    Some(p) => {
                        vs.push(disk_vec(&mut st, *id)?);
                        ps.push(p.payload());
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
                // 내적은 비슷한 문서 찾기와 같은 dot(AVX) — 예전 한 칸씩 더하기보다 몇 배 빠르다
                .map(|(i, p)| (p.vec.dot(&q), *i)).collect();
            let cmp = |a: &(f32, i64), b: &(f32, i64)| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1));
            // 28만 개를 다 정렬하지 않고 상위 limit 개만 골라 정렬한다 — 비교에 id 가 들어가 동점이 없으므로
            //   고른 묶음 · 순서가 전체 정렬과 똑같다
            if limit < scored.len() {
                scored.select_nth_unstable_by(limit, cmp);
                scored.truncate(limit);
            }
            scored.sort_by(cmp);
            // 돌려줄 점만 payload 를 푼다
            Ok(scored.into_iter().map(|(s, i)| (i, s, inn.points[&i].payload())).collect())
        })
    }

    //------------------------------------------------------------------
    // 문서 하나의 점 — (id, 벡터, payload) chunk_idx 순 (비슷한 문서 찾기의 기준 문서)
    //=> 경로는 normcase 로 비교한다(대소문자 · 구분자 차이 무시). 벡터 대기 점은 빈 벡터로 온다.
    //
    // -in: doc_path = 문서 경로
    //
    // -out: 점 목록(없으면 빈 목록)
    // -out: error = 저장소 적재 실패면 Err
    //------------------------------------------------------------------
    pub fn doc_points(&self, doc_path: &str) -> Result<Vec<(i64, Vec<f32>, Payload)>, String> {
        let want = pyutil::normcase(doc_path);
        self.with(|inn| {
            let mut v: Vec<(i64, &Point)> = inn.points.iter()
                .filter(|(_, p)| *p.doc.norm == *want)
                .map(|(i, p)| (*i, p)).collect();
            // 문서 순서대로 — 고른 간격으로 뽑을 때 문서 전체에 고르게 퍼지게
            v.sort_by_key(|(i, p)| (p.chunk_idx, *i));
            // 기준 문서 벡터는 질의로만 쓴다 — f16 이면 f32 로 풀어 준다
            Ok(v.into_iter().map(|(i, p)| (i, p.vec.to_f32(), p.payload())).collect())
        })
    }

    //------------------------------------------------------------------
    // 내용 해시로 문서 찾기 — 인덱스에 같은 내용의 문서가 있으면 그 경로(경로 순 첫 문서)
    //=> 인덱싱 폴더 밖의 파일이라도 내용이 같으면 그 문서의 저장 벡터를 쓸 수 있다.
    //
    // -in: hash = 파일 내용 sha1
    //
    // -out: Some(문서 경로) / 없으면 None
    // -out: error = 저장소 적재 실패면 Err
    //------------------------------------------------------------------
    pub fn doc_by_hash(&self, hash: &str) -> Result<Option<String>, String> {
        if hash.is_empty() {
            return Ok(None);
        }
        self.with(|inn| {
            Ok(inn.points.values().filter(|p| &*p.doc.content_hash == hash)
                .map(|p| p.doc.path.to_string()).min())
        })
    }

    //------------------------------------------------------------------
    // 문서별 최고 점수 훑기 (비슷한 문서 찾기 · 설계서 §5-③)
    //=> 기준 문서에서 고른 청크 벡터(qvs) 하나하나에 대해, 문서마다 "가장 비슷한 청크 점수와 그 id" 를 구한다.
    //   같은 훑기에서 본문이 기준 문서와 그대로 같은 청크도 센다(사본 · 다른 판 판정, §5-①).
    //    1) 점을 스레드 수만큼 나눠 동시에 훑는다(점 21만 × 질의 32 — 한 줄이면 1초 넘게 걸린다)
    //    2) 스레드마다 문서 → (질의별 최고, 같은 본문 집합) 표를 만들고 끝에 합친다
    //    3) 벡터 대기 점은 내적을 건너뛰지만 청크 수 · 본문 비교에는 넣는다
    //
    // -in: qvs      = 질의 벡터들(정규화 안 됐어도 됨)
    // -in: bodies   = 기준 문서 본문 열쇠 집합 / body_key = 청크 글 → 본문 열쇠(짧으면 None)
    // -in: allow    = Some(허용 id) 이면 그 안에서만(폴더 한정) / threads = 스레드 수(1 이상)
    //
    // -out: DocScan = 문서별 결과
    // -out: error = 저장소 적재 실패면 Err
    //------------------------------------------------------------------
    pub fn doc_max_scan(&self, qvs: &[Vec<f32>], bodies: &HashSet<u64>, body_key: fn(&str) -> Option<u64>,
                        allow: Option<&HashSet<i64>>, threads: usize) -> Result<DocScan, String> {
        let qs: Vec<Vec<f32>> = qvs.iter().map(|q| {
            let mut q = q.clone();
            normalize(&mut q);
            q
        }).collect();
        self.with(|inn| {
            let pts: Vec<(&i64, &Point)> = inn.points.iter().filter(|(i, _)| allow.map_or(true, |a| a.contains(i))).collect();
            let nt = threads.max(1).min(pts.len().max(1));
            let per = pts.len().div_ceil(nt).max(1);
            let parts: Vec<DocScan> = std::thread::scope(|s| {
                let hs: Vec<_> = pts.chunks(per).map(|part| {
                    let qs = &qs;
                    s.spawn(move || scan_part(part, qs, bodies, body_key))
                }).collect();
                hs.into_iter().map(|h| h.join().unwrap_or_default()).collect()
            });
            // 스레드 결과 합치기 — 같은 문서는 질의별 더 높은 점수 · 본문 집합 합집합 · 청크 수 합
            let mut out = DocScan::default();
            let mut at: HashMap<String, usize> = HashMap::new();
            for p in parts {
                for (k, d) in p.docs.into_iter().enumerate() {
                    match at.get(&d) {
                        None => {
                            at.insert(d.clone(), out.docs.len());
                            out.docs.push(d);
                            out.best.push(p.best[k].clone());
                            out.same.push(p.same[k].clone());
                            out.chunks.push(p.chunks[k]);
                            out.first.push(p.first[k].clone());
                        }
                        Some(&j) => {
                            for (q, b) in p.best[k].iter().enumerate() {
                                if b.0 > out.best[j][q].0 {
                                    out.best[j][q] = *b;
                                }
                            }
                            let add = p.same[k].clone();
                            out.same[j].extend(add);
                            out.chunks[j] += p.chunks[k];
                            // 문서 대표 payload 는 id 가 작은 쪽(처음 청크에 가까운 쪽)
                            if p.first[k].0 < out.first[j].0 {
                                out.first[j] = p.first[k].clone();
                            }
                        }
                    }
                }
            }
            // 대표 payload 는 문서마다 한 번만 푼다(A안 — 점마다 복사하지 않는다)
            for f in out.first.iter_mut() {
                if let Some(pt) = inn.points.get(&f.0) {
                    f.1 = pt.payload();
                }
            }
            Ok(out)
        })
    }

    //------------------------------------------------------------------
    // id 들의 payload (없는 id 는 빠진다)
    //------------------------------------------------------------------
    pub fn payloads(&self, ids: &[i64]) -> Result<HashMap<i64, Payload>, String> {
        self.with(|inn| Ok(ids.iter().filter_map(|i| inn.points.get(i).map(|p| (*i, p.payload()))).collect()))
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
                // normcase 는 문서 열쇠에 미리 구해 둔 값
                if under(&p.doc.norm, &f) {
                    docs.insert(p.doc.path.to_string());
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
                let d: &str = &p.doc.norm;
                if want.contains(d) && f.as_ref().map_or(true, |f| under(d, f)) {
                    seen.insert(d.to_string());
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
            let mut v: Vec<&Point> = inn.points.values()
                .filter(|p| &*p.doc.path == doc_path && p.section_id == Some(section_id))
                .collect();
            v.sort_by_key(|p| p.chunk_idx);
            Ok(v.into_iter().map(|p| p.payload()).collect())
        })
    }

    //------------------------------------------------------------------
    // 모든 점의 (id, 본문, doc_path) — id 순 (BM25 재구축·고아 청소)
    //------------------------------------------------------------------
    pub fn scroll_all(&self) -> Result<Vec<(i64, String, String)>, String> {
        self.with(|inn| Ok(inn.points.iter().map(|(i, p)| (*i, p.text.to_string(), p.doc.path.to_string())).collect()))
    }

    //------------------------------------------------------------------
    // 모든 점의 (id, doc_path) — id 순 (고아 청소 — 본문은 읽지 않는다)
    //------------------------------------------------------------------
    pub fn doc_paths(&self) -> Result<Vec<(i64, String)>, String> {
        self.with(|inn| Ok(inn.points.iter().map(|(i, p)| (*i, p.doc.path.to_string())).collect()))
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
                let text: &str = &p.text;
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
                    out.push((*i, mask, p.doc.path.to_string()));
                }
            }
            Ok(out)
        })
    }

    pub fn texts_of(&self, ids: &[i64]) -> Result<Vec<String>, String> {
        self.with(|inn| Ok(ids.iter().map(|i| inn.points.get(i).map(|p| p.text.to_string()).unwrap_or_default()).collect()))
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
            .map(|(i, p)| (*i, p.text.to_string(), p.doc.path.to_string())).collect()))
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
                    p.vec = Vector::new(inn.f16, vv);
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

#[cfg(test)]
mod tests {
    use super::*;

    // 시험용 payload — 문서 경로 · 본문 · 청크 번호
    fn pl(doc: &str, text: &str, ci: i64) -> Payload {
        let mut m = Payload::new();
        m.insert("doc_path".into(), Value::String(doc.into()));
        m.insert("text".into(), Value::String(text.into()));
        m.insert("chunk_idx".into(), Value::from(ci));
        m.insert("content_hash".into(), Value::String(format!("h-{}", doc)));
        m
    }

    //------------------------------------------------------------------
    // f16 이 디스크로 새지 않는가 (메모리 절감 설계서 B안 §5-4)
    //=> 메모리는 f16(설정 기본)이어도, 문서 복사(points_of) · 재구축 재사용(take_vectors)이 돌려주는 벡터는
    //   디스크 f32 를 정규화한 값과 '비트까지' 같아야 한다. f16 왕복 값이면 끝자리가 달라 실패한다.
    //   payload 는 넣은 것과 같고(A안), 검색 · 기준 문서 벡터도 f16 오차 안에서 맞아야 한다.
    //------------------------------------------------------------------
    #[test]
    fn disk_vectors_not_rounded() {
        let dir = std::env::temp_dir().join(format!("srag_store_f16_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let store = VectorStore::new(Some(dir.to_string_lossy().to_string()));
        // f16 으로 딱 떨어지지 않는 값들
        let raw: Vec<Vec<f32>> = (0..3).map(|k| (0..16).map(|i| 0.1 + (i as f32) * 0.0123 + k as f32 * 0.37).collect()).collect();
        let pls = vec![pl("D:/a/x.txt", "가 나", 0), pl("D:/a/x.txt", "다 라", 1), pl("D:/b/y.txt", "마", 0)];
        store.upsert(&[1, 2, 3], &raw, &pls).unwrap();
        let norm = |v: &Vec<f32>| {
            let mut w = v.clone();
            normalize(&mut w);
            w
        };
        // payload 그대로
        let got = store.payloads(&[1, 2, 3]).unwrap();
        assert_eq!(got[&2], pls[1]);
        // 문서 복사 — 디스크 f32 정규화 값과 비트까지 같다
        let (vs, ps) = store.points_of(&[1, 3]).unwrap().unwrap();
        assert_eq!(vs[0], norm(&raw[0]));
        assert_eq!(vs[1], norm(&raw[2]));
        assert_eq!(ps[1], pls[2]);
        // 메모리가 f16 이면 기준 문서 벡터는 f16 오차 안
        let dp = store.doc_points("d:/a/x.txt").unwrap();
        assert_eq!(dp.len(), 2);
        for (a, b) in dp[0].1.iter().zip(norm(&raw[0]).iter()) {
            assert!((a - b).abs() < 2e-3);
        }
        // 검색 — 자기 자신이 1등
        let hit = store.search(&raw[2], 1, None).unwrap();
        assert_eq!(hit[0].0, 3);
        // 재구축 재사용 — 지문(본문)별 벡터도 디스크 값 그대로
        let (pool, samples) = store.take_vectors(|p| pl_str(p, "text").to_string(), 2).unwrap();
        assert_eq!(pool["다 라"], norm(&raw[1]));
        assert!(!samples.is_empty());
        store.close();
        let _ = std::fs::remove_dir_all(&dir);
    }
}
