//------------------------------------------------------------------
// dense 검색 속도만 재기 (진단용 예제)
//=> store.search(10) 과 doc_max_scan(질의 32) 만 되풀이해 질문당 시간을 잰다.
//   SIMPLERAG_NO_AVX=1 로 한 번 더 돌려 AVX 몫을 나눠 본다. 결과 id 는 파일로 남겨 두 실행을 맞댄다.
//
// 실행: cargo run --release --example search_bench -- <rag_index 폴더> <질문 파일> <결과 파일>
//------------------------------------------------------------------

use std::collections::HashSet;
use std::io::Write;
use std::time::Instant;

use simplerag::embed::Embedder;
use simplerag::store::VectorStore;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let qs: Vec<String> = std::fs::read_to_string(&a[2]).unwrap().lines().filter(|l| !l.trim().is_empty()).map(|s| s.to_string()).collect();
    let store = VectorStore::new(Some(a[1].clone()));
    store.ensure_loaded().unwrap();
    let emb = Embedder::new();
    emb.ensure_loaded().unwrap();
    let qv: Vec<Vec<f32>> = qs.iter().map(|q| emb.embed_query(q).unwrap()).collect();
    let mut out = std::fs::File::create(&a[3]).unwrap();
    // 한 번 데워 두고(캐시) 잰다
    store.search(&qv[0], 10, None).unwrap();
    let t = Instant::now();
    for q in &qv {
        let ids: Vec<String> = store.search(q, 10, None).unwrap().into_iter().map(|x| x.0.to_string()).collect();
        writeln!(out, "{}", ids.join(",")).unwrap();
    }
    let ts = t.elapsed().as_secs_f64() / qv.len() as f64 * 1000.0;
    // 예전 search 와 같은 계산 — 한 칸씩 더한 점수 + 28만 개 전체 정렬(같은 프로세스·같은 벡터로)
    let pts = store.scroll_all().unwrap().len();
    let vecs: Vec<Vec<f32>> = {
        let conn = rusqlite::Connection::open_with_flags(format!("{}\\points.sqlite", a[1]), rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
        let mut st = conn.prepare("SELECT vec FROM points ORDER BY id").unwrap();
        let rows: Vec<Vec<u8>> = st.query_map([], |r| r.get(0)).unwrap().flatten().collect();
        rows.iter().filter(|b| !b.is_empty()).map(|b| {
            let mut v: Vec<f32> = b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
            let n = v.iter().map(|x| x * x).sum::<f32>().sqrt();
            v.iter_mut().for_each(|x| *x /= n.max(1e-12));
            v
        }).collect()
    };
    let t = Instant::now();
    for q in qv.iter().take(50) {
        let mut sc: Vec<(f32, i64)> = vecs.iter().enumerate().map(|(i, v)| (v.iter().zip(q).map(|(a, b)| a * b).sum::<f32>(), i as i64)).collect();
        sc.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap().then(a.1.cmp(&b.1)));
        sc.truncate(10);
        std::hint::black_box(sc);
    }
    let t_old = t.elapsed().as_secs_f64() / 50.0 * 1000.0;
    println!("점 {} · 예전 search 계산(한 칸씩 더하기 + 전체 정렬) 질문당 {:.1} ms", pts, t_old);
    let q32: Vec<Vec<f32>> = qv.iter().take(32).cloned().collect();
    let mut tm = vec![];
    for _ in 0..3 {
        let t = Instant::now();
        store.doc_max_scan(&q32, &HashSet::new(), |_| None, None, 8).unwrap();
        tm.push(t.elapsed().as_secs_f64() * 1000.0);
    }
    tm.sort_by(|a, b| a.partial_cmp(b).unwrap());
    println!("AVX {} · search(10) 질문당 {:.1} ms · doc_max_scan(질의 32) 중앙 {:.0} ms",
        std::env::var("SIMPLERAG_NO_AVX").map(|v| v != "1").unwrap_or(true), ts, tm[1]);
}
