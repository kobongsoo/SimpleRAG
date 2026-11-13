//------------------------------------------------------------------
// 벡터 저장소 보관 방식별 메모리 비교 (진단용 예제)
//=> 지금 저장소는 점마다 payload 를 serde_json Map 으로 들고 있다. 디스크 JSON 은 242MB 인데
//   램은 1.1GB 가까이 먹으므로, 보관 방식을 바꾸면 얼마나 주는지 잰다.
//    map     = 지금 방식(Vec<f32> + serde_json Map)
//    raw     = payload 를 JSON 글자 그대로(Box<str>) — 필요할 때 풀기
//    compact = 문서 단위 필드는 문서 표 하나에 모으고, 점에는 본문·청크 필드만
//    vec     = 벡터만(payload 없음) — 바닥값
//   각 방식은 프로세스를 따로 띄워 잰다(앞 방식 메모리가 섞이지 않게).
//
// 실행: cargo run --release --example store_mem -- <points.sqlite> <map|raw|compact|vec>
//------------------------------------------------------------------

use std::collections::{BTreeMap, HashMap};
use std::process::Command;
use std::sync::Arc;
use std::time::Instant;

use rusqlite::{Connection, OpenFlags};
use serde_json::{Map, Value};

// 이 프로세스 전용 메모리(MB)
fn private_mb() -> f64 {
    let pid = std::process::id();
    let ps = format!("[int]((Get-Process -Id {pid}).PrivateMemorySize64/1MB)");
    let out = Command::new("powershell").args(["-NoProfile", "-Command", &ps]).output().unwrap();
    String::from_utf8_lossy(&out.stdout).trim().parse().unwrap_or(0.0)
}

// f32 묶음 → 벡터
fn from_blob(b: &[u8]) -> Vec<f32> {
    b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()
}

// 문서 단위 필드 묶음(같은 문서의 청크가 함께 쓴다)
#[allow(dead_code)]
struct Doc {
    doc_path: Box<str>,
    doc_name: Box<str>,
    folder: Box<str>,
    path: Option<Box<str>>,
    source: Box<str>,
    content_hash: Box<str>,
    gen: Box<str>,
    mtime: f64,
}

// 청크 단위 필드만 남긴 점
#[allow(dead_code)]
struct Compact {
    vec: Vec<f32>,
    doc: Arc<Doc>,
    text: Box<str>,
    chunk_hash: Box<str>,
    chunk_idx: i32,
    section_id: Option<Box<str>>,
    page: i32,
    page_from: i32,
    page_to: i32,
}

// payload 문자열 필드 → Box<str>
fn s(m: &Map<String, Value>, k: &str) -> Box<str> {
    match m.get(k) {
        Some(Value::String(x)) => x.as_str().into(),
        Some(v) => v.to_string().into(),
        None => "".into(),
    }
}

// payload 정수 필드(없으면 -1)
fn i(m: &Map<String, Value>, k: &str) -> i32 {
    m.get(k).and_then(|v| v.as_i64()).map(|x| x as i32).unwrap_or(-1)
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (path, mode) = (&a[1], a[2].as_str());
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let base = private_mb();
    let t = Instant::now();
    let mut st = conn.prepare("SELECT id, vec, payload FROM points").unwrap();
    let mut rows = st.query([]).unwrap();
    let mut n = 0usize;
    // 방식마다 담는 그릇 — 끝까지 살려 두고 잰다
    let mut m_map: BTreeMap<i64, (Vec<f32>, Map<String, Value>)> = BTreeMap::new();
    let mut m_raw: BTreeMap<i64, (Vec<f32>, Box<str>)> = BTreeMap::new();
    let mut m_cmp: BTreeMap<i64, Compact> = BTreeMap::new();
    let mut m_vec: BTreeMap<i64, Vec<f32>> = BTreeMap::new();
    // hybrid = payload 원문 JSON + 훑기에 쓰는 필드(본문·문서 경로 공유·청크 번호)만 따로
    #[allow(clippy::type_complexity)]
    let mut m_hyb: BTreeMap<i64, (Vec<f32>, Box<str>, Box<str>, Arc<str>, i32, i32)> = BTreeMap::new();
    let mut paths: HashMap<String, Arc<str>> = HashMap::new();
    let mut docs: HashMap<String, Arc<Doc>> = HashMap::new();
    while let Some(r) = rows.next().unwrap() {
        let id: i64 = r.get(0).unwrap();
        let v = from_blob(&r.get::<_, Vec<u8>>(1).unwrap());
        let pl: String = r.get(2).unwrap();
        n += 1;
        match mode {
            "map" => {
                m_map.insert(id, (v, serde_json::from_str(&pl).unwrap_or_default()));
            }
            "raw" => {
                m_raw.insert(id, (v, pl.into_boxed_str()));
            }
            "compact" => {
                let m: Map<String, Value> = serde_json::from_str(&pl).unwrap_or_default();
                // 문서 열쇠 = 경로 + 내용 지문(같은 문서면 문서 필드를 하나만 둔다)
                let key = format!("{}|{}", s(&m, "doc_path"), s(&m, "content_hash"));
                let d = docs.entry(key).or_insert_with(|| Arc::new(Doc {
                    doc_path: s(&m, "doc_path"), doc_name: s(&m, "doc_name"), folder: s(&m, "folder"),
                    path: m.get("path").map(|_| s(&m, "path")), source: s(&m, "source"),
                    content_hash: s(&m, "content_hash"), gen: s(&m, "gen"),
                    mtime: m.get("mtime").and_then(|x| x.as_f64()).unwrap_or(0.0),
                })).clone();
                m_cmp.insert(id, Compact {
                    vec: v, doc: d, text: s(&m, "text"), chunk_hash: s(&m, "chunk_hash"), chunk_idx: i(&m, "chunk_idx"),
                    section_id: m.get("section_id").map(|_| s(&m, "section_id")),
                    page: i(&m, "page"), page_from: i(&m, "page_from"), page_to: i(&m, "page_to"),
                });
            }
            "hybrid" => {
                let m: Map<String, Value> = serde_json::from_str(&pl).unwrap_or_default();
                // 같은 문서 경로는 한 벌만 두고 청크들이 나눠 가진다
                let dp = s(&m, "doc_path");
                let d = paths.entry(dp.to_string()).or_insert_with(|| Arc::from(&*dp)).clone();
                m_hyb.insert(id, (v, pl.into_boxed_str(), s(&m, "text"), d, i(&m, "chunk_idx"), i(&m, "section_id")));
            }
            _ => {
                m_vec.insert(id, v);
            }
        }
    }
    let load = t.elapsed().as_secs_f64();
    let used = private_mb() - base;
    println!("{:<8} 점 {} · 문서 {} · 적재 {:.1}s · 늘어난 전용 메모리 {:.0} MB", mode, n, docs.len(), load, used);
    std::hint::black_box((&m_map, &m_raw, &m_cmp, &m_vec, &m_hyb));
    // hybrid 에서 payload 를 돌려줄 때 드는 값 — 원문 JSON 풀기 1천 번
    if mode == "hybrid" {
        let t = Instant::now();
        let k: usize = m_hyb.values().take(1000).map(|x| serde_json::from_str::<Map<String, Value>>(&x.1).map(|m| m.len()).unwrap_or(0)).sum();
        println!("payload 1,000개 풀기 {:.1} ms ({})", t.elapsed().as_secs_f64() * 1000.0, k);
    }
}
