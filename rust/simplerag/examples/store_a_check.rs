//------------------------------------------------------------------
// 저장소 A안(payload 원문 보관) 동일성 검사 (진단용 예제)
//=> 같은 points.sqlite 를 예전 방식(점마다 serde_json Map)으로 따로 읽어 '기준'을 만들고,
//   새 저장소(VectorStore)가 돌려주는 값과 하나하나 맞댄다. 하나라도 다르면 실패를 센다.
//    1) payloads(전체 id) — Map 이 기준과 같은가(필드 순서까지)
//    2) scroll_all · doc_paths · texts_of — 본문 · 경로가 같은가
//    3) doc_points · section_chunks — 문서 표본 50개
//    4) folder_scope · docs_scope · doc_by_hash · scan_words — 기준 계산과 같은가
//    5) doc_max_scan — 대표 payload · 청크 수
//   메모리도 함께 잰다(적재 뒤 전용 메모리 증가).
//
// 실행: SIMPLERAG_HOME 을 설치 폴더로 맞춘 뒤
//       cargo run --release --example store_a_check -- <rag_index 폴더>
//------------------------------------------------------------------

use std::collections::{BTreeMap, HashMap, HashSet};
use std::process::Command;
use std::time::Instant;

use rusqlite::{Connection, OpenFlags};
use serde_json::{Map, Value};
use simplerag::pyutil;
use simplerag::store::{norm_dir, pl_str, under, VectorStore};

type Payload = Map<String, Value>;

// 이 프로세스 전용 메모리(MB)
fn private_mb() -> f64 {
    let ps = format!("[int]((Get-Process -Id {}).PrivateMemorySize64/1MB)", std::process::id());
    let out = Command::new("powershell").args(["-NoProfile", "-Command", &ps]).output().unwrap();
    String::from_utf8_lossy(&out.stdout).trim().parse().unwrap_or(0.0)
}

// 기준과 다르면 실패를 세고 처음 몇 개만 보여 준다
struct Check {
    fails: usize,
    total: usize,
}
impl Check {
    fn eq<T: PartialEq + std::fmt::Debug>(&mut self, what: &str, a: &T, b: &T) {
        self.total += 1;
        if a != b {
            self.fails += 1;
            if self.fails <= 5 {
                println!("  ✗ {} 다름\n    기준: {:?}\n    새것: {:?}", what, format!("{:?}", a).chars().take(200).collect::<String>(),
                    format!("{:?}", b).chars().take(200).collect::<String>());
            }
        }
    }
}

fn main() {
    let dir = std::env::args().nth(1).expect("rag_index 폴더");
    // 1) 기준 — 예전 방식으로 읽기(id 순)
    let conn = Connection::open_with_flags(format!("{}\\points.sqlite", dir), OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let mut base: BTreeMap<i64, Payload> = BTreeMap::new();
    {
        let mut st = conn.prepare("SELECT id, payload FROM points").unwrap();
        let mut rows = st.query([]).unwrap();
        while let Some(r) = rows.next().unwrap() {
            let s: String = r.get(1).unwrap();
            base.insert(r.get(0).unwrap(), serde_json::from_str(&s).unwrap_or_default());
        }
    }
    drop(conn);
    println!("기준 점 {}", base.len());

    // 2) 새 저장소 적재 — 메모리 증가를 잰다(기준 Map 을 먼저 들고 있으므로 차이로 본다)
    let m0 = private_mb();
    let t = Instant::now();
    let store = VectorStore::new(Some(dir.clone()));
    store.ensure_loaded().unwrap();
    println!("새 저장소 적재 {:.1}s · 전용 메모리 +{:.0} MB", t.elapsed().as_secs_f64(), private_mb() - m0);

    let mut c = Check { fails: 0, total: 0 };
    let ids: Vec<i64> = base.keys().copied().collect();

    // payloads 전체
    let got = store.payloads(&ids).unwrap();
    let mut n_same = 0;
    for (i, p) in &base {
        let g = got.get(i);
        c.eq("payloads 있음", &true, &g.is_some());
        if let Some(g) = g {
            // 필드 순서까지 — 키 목록과 값을 함께 본다
            let ka: Vec<&String> = p.keys().collect();
            let kb: Vec<&String> = g.keys().collect();
            c.eq(&format!("payload 키 순서 id={}", i), &ka, &kb);
            c.eq(&format!("payload id={}", i), p, g);
            if p == g && ka == kb {
                n_same += 1;
            }
        }
    }
    println!("payloads: {}/{} 같음", n_same, base.len());

    // scroll_all · doc_paths · texts_of
    let sa = store.scroll_all().unwrap();
    let want: Vec<(i64, String, String)> = base.iter().map(|(i, p)| (*i, pl_str(p, "text").to_string(), pl_str(p, "doc_path").to_string())).collect();
    c.eq("scroll_all", &want, &sa);
    let dp = store.doc_paths().unwrap();
    let want: Vec<(i64, String)> = base.iter().map(|(i, p)| (*i, pl_str(p, "doc_path").to_string())).collect();
    c.eq("doc_paths", &want, &dp);
    let sample: Vec<i64> = ids.iter().step_by(997).copied().collect();
    let want: Vec<String> = sample.iter().map(|i| pl_str(&base[i], "text").to_string()).collect();
    c.eq("texts_of", &want, &store.texts_of(&sample).unwrap());

    // 문서 표본 50개 — doc_points · section_chunks · doc_by_hash
    let mut docs: Vec<String> = base.values().map(|p| pl_str(p, "doc_path").to_string()).collect::<HashSet<_>>().into_iter().collect();
    docs.sort();
    let step = (docs.len() / 50).max(1);
    for d in docs.iter().step_by(step).take(50) {
        // doc_points 기준: normcase 같음, (chunk_idx, id) 순
        let nd = pyutil::normcase(d);
        let mut want: Vec<(i64, Payload)> = base.iter().filter(|(_, p)| pyutil::normcase(pl_str(p, "doc_path")) == nd).map(|(i, p)| (*i, p.clone())).collect();
        want.sort_by_key(|(i, p)| (p.get("chunk_idx").and_then(|x| x.as_i64()).unwrap_or(0), *i));
        let got: Vec<(i64, Payload)> = store.doc_points(d).unwrap().into_iter().map(|(i, _, p)| (i, p)).collect();
        c.eq(&format!("doc_points {}", d), &want, &got);
        // section_chunks — 그 문서의 첫 절
        if let Some(sid) = want.iter().find_map(|(_, p)| p.get("section_id").and_then(|x| x.as_i64())) {
            let mut w: Vec<Payload> = base.values().filter(|p| pl_str(p, "doc_path") == d && p.get("section_id").and_then(|x| x.as_i64()) == Some(sid)).cloned().collect();
            w.sort_by_key(|p| p.get("chunk_idx").and_then(|x| x.as_i64()).unwrap_or(0));
            c.eq(&format!("section_chunks {} #{}", d, sid), &w, &store.section_chunks(d, sid).unwrap());
        }
        // doc_by_hash
        if let Some((_, p)) = want.first() {
            let h = pl_str(p, "content_hash");
            let w = base.values().filter(|p| pl_str(p, "content_hash") == h).map(|p| pl_str(p, "doc_path").to_string()).min();
            let w = if h.is_empty() { None } else { w };
            c.eq(&format!("doc_by_hash {}", h), &w, &store.doc_by_hash(h).unwrap());
        }
    }

    // folder_scope · docs_scope — 문서 경로의 상위 폴더 표본
    let mut folders: Vec<String> = docs.iter().filter_map(|d| std::path::Path::new(d).parent().map(|p| p.to_string_lossy().to_string())).collect::<HashSet<_>>().into_iter().collect();
    folders.sort();
    for f in folders.iter().step_by((folders.len() / 20).max(1)).take(20) {
        let nf = norm_dir(f);
        let mut wd = HashSet::new();
        let mut wi = HashSet::new();
        for (i, p) in &base {
            let d = pl_str(p, "doc_path");
            if under(&pyutil::normcase(d), &nf) {
                wd.insert(d.to_string());
                wi.insert(*i);
            }
        }
        let (n, got) = store.folder_scope(f).unwrap();
        c.eq(&format!("folder_scope {}", f), &(wd.len(), wi), &(n, got));
    }
    let pick: HashSet<String> = docs.iter().step_by(step).take(30).cloned().collect();
    let wantn: HashSet<String> = pick.iter().map(|d| pyutil::normcase(d)).collect();
    let mut seen = HashSet::new();
    let mut wi = HashSet::new();
    for (i, p) in &base {
        let d = pyutil::normcase(pl_str(p, "doc_path"));
        if wantn.contains(&d) {
            seen.insert(d);
            wi.insert(*i);
        }
    }
    c.eq("docs_scope", &(seen.len(), wi), &store.docs_scope(&pick, None).unwrap());

    // scan_words — 흔한 낱말 몇 개(본문에서 앞 접두 줄 뺀 곳)
    for w in [["휴가"], ["계약"], ["AI"]] {
        let ws: Vec<String> = w.iter().map(|s| s.to_string()).collect();
        let got = store.scan_words(&ws, None).unwrap();
        let low = ws[0].to_ascii_lowercase();
        let ascii = low.bytes().all(|b| b.is_ascii_alphanumeric());
        let mut want = vec![];
        for (i, p) in &base {
            let text = pl_str(p, "text");
            let body = if text.starts_with('[') { text.find("]\n").map_or(text, |k| &text[k + 2..]) } else { text };
            let hay = if ascii { body.to_ascii_lowercase() } else { body.to_string() };
            let hit = if ascii {
                let b = hay.as_bytes();
                hay.match_indices(&low).any(|(k, _)| (k == 0 || !b[k - 1].is_ascii_alphanumeric()) && (k + low.len() >= b.len() || !b[k + low.len()].is_ascii_alphanumeric()))
            } else {
                hay.contains(&low)
            };
            if hit {
                want.push((*i, 1u32, pl_str(p, "doc_path").to_string()));
            }
        }
        c.eq(&format!("scan_words {:?}", w), &want, &got);
    }

    // doc_max_scan — 대표 payload(가장 작은 id 의 payload) · 청크 수
    let q = vec![vec![0.05f32; 384]];
    let scan = store.doc_max_scan(&q, &HashSet::new(), |_| None, None, 4).unwrap();
    let mut first: HashMap<String, (i64, usize)> = HashMap::new();
    for (i, p) in &base {
        let e = first.entry(pl_str(p, "doc_path").to_string()).or_insert((*i, 0));
        e.1 += 1;
    }
    for (j, d) in scan.docs.iter().enumerate() {
        let (fi, n) = first[d];
        c.eq(&format!("doc_max_scan 청크 수 {}", d), &n, &scan.chunks[j]);
        c.eq(&format!("doc_max_scan 대표 {}", d), &(fi, base[&fi].clone()), &scan.first[j]);
    }
    c.eq("doc_max_scan 문서 수", &first.len(), &scan.docs.len());

    println!("검사 {} 개 중 {} 개 다름", c.total, c.fails);
    std::process::exit(if c.fails == 0 { 0 } else { 1 });
}
