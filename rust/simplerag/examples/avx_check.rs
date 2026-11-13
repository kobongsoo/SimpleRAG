//------------------------------------------------------------------
// AVX 내적 검사 — 검색 순위가 예전과 같은가 · 얼마나 빨라졌나 (진단용 예제)
//=> 저장소 search() 와 doc_max_scan() 이 쓰는 내적을 AVX·FMA 판으로 바꿨다.
//   더하는 순서가 달라 점수 끝자리가 바뀔 수 있으므로, 골든 질문으로 상위 순위를 예전 방식과 맞댄다.
//    1) 예전 search 방식(한 칸씩 더하기)으로 상위 10 · 50 을 직접 계산 = 기준
//    2) store.search(10 · 50) 결과와 id 목록 비교
//    3) 시간: 기준 계산 vs store.search
//    4) doc_max_scan(질의 32개) 의 문서별 최고 점수 상위 20 을 출력 — SIMPLERAG_NO_AVX=1 로 한 번 더 돌려 비교
//
// 실행: SIMPLERAG_HOME 을 설치 폴더로 맞춘 뒤
//       cargo run --release --example avx_check -- <rag_index 폴더> <질문 파일>
//------------------------------------------------------------------

use std::collections::HashSet;
use std::time::Instant;

use rusqlite::{Connection, OpenFlags};
use simplerag::embed::Embedder;
use simplerag::store::VectorStore;

// 벡터 정규화
fn normalize(v: &mut [f32]) {
    let n = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if n > 1e-12 {
        v.iter_mut().for_each(|x| *x /= n);
    }
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let dir = &a[1];
    let qs: Vec<String> = std::fs::read_to_string(&a[2]).unwrap().lines().filter(|l| !l.trim().is_empty()).map(|s| s.to_string()).collect();

    // 기준용 벡터 — 저장소와 같이 정규화(id 순)
    let conn = Connection::open_with_flags(format!("{}\\points.sqlite", dir), OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let mut pts: Vec<(i64, Vec<f32>)> = vec![];
    {
        let mut st = conn.prepare("SELECT id, vec FROM points ORDER BY id").unwrap();
        let mut rows = st.query([]).unwrap();
        while let Some(r) = rows.next().unwrap() {
            let b: Vec<u8> = r.get(1).unwrap();
            if b.is_empty() {
                continue;
            }
            let mut v: Vec<f32> = b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
            normalize(&mut v);
            pts.push((r.get(0).unwrap(), v));
        }
    }
    let store = VectorStore::new(Some(dir.clone()));
    store.ensure_loaded().unwrap();
    let emb = Embedder::new();
    emb.ensure_loaded().unwrap();
    let qv: Vec<Vec<f32>> = qs.iter().map(|q| {
        let mut v = emb.embed_query(q).unwrap();
        normalize(&mut v);
        v
    }).collect();

    let (mut t_old, mut t_new) = (0f64, 0f64);
    let mut same = [0usize; 2];
    let mut same_set = [0usize; 2];
    for q in &qv {
        // 예전 search: 한 칸씩 더한 점수, (점수 내림, id 오름)
        let t = Instant::now();
        let mut sc: Vec<(f32, i64)> = pts.iter().map(|(i, v)| (v.iter().zip(q).map(|(a, b)| a * b).sum::<f32>(), *i)).collect();
        sc.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap().then(a.1.cmp(&b.1)));
        t_old += t.elapsed().as_secs_f64();
        for (k, lim) in [10usize, 50].iter().enumerate() {
            let want: Vec<i64> = sc.iter().take(*lim).map(|x| x.1).collect();
            let t = Instant::now();
            let got: Vec<i64> = store.search(q, *lim, None).unwrap().into_iter().map(|x| x.0).collect();
            if k == 0 {
                t_new += t.elapsed().as_secs_f64();
            }
            if want == got {
                same[k] += 1;
            }
            let ws: HashSet<_> = want.iter().collect();
            if got.iter().all(|i| ws.contains(i)) {
                same_set[k] += 1;
            }
        }
    }
    let n = qv.len();
    println!("질문 {} 개 · AVX {}", n, std::env::var("SIMPLERAG_NO_AVX").map(|v| v != "1").unwrap_or(true));
    println!("search 상위 10: 순서까지 같음 {}/{} · 묶음 같음 {}/{}", same[0], n, same_set[0], n);
    println!("search 상위 50: 순서까지 같음 {}/{} · 묶음 같음 {}/{}", same[1], n, same_set[1], n);
    println!("질문당 시간: 예전 방식(정렬 포함) {:.1} ms · store.search(10) {:.1} ms", t_old / n as f64 * 1000.0, t_new / n as f64 * 1000.0);

    // doc_max_scan — 질의 32개(비슷한 문서 찾기와 같은 규모)
    let q32: Vec<Vec<f32>> = qv.iter().take(32).cloned().collect();
    let t = Instant::now();
    let scan = store.doc_max_scan(&q32, &HashSet::new(), |_| None, None, 8).unwrap();
    let el = t.elapsed().as_secs_f64();
    let mut rank: Vec<(f32, String)> = scan.docs.iter().enumerate().map(|(j, d)| (scan.best[j].iter().map(|b| b.0).sum::<f32>(), d.clone())).collect();
    rank.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap().then(a.1.cmp(&b.1)));
    println!("doc_max_scan(질의 32 · 스레드 8) {:.0} ms", el * 1000.0);
    for (s, d) in rank.iter().take(20) {
        println!("  @rank {:.5} {}", s, d);
    }
}
