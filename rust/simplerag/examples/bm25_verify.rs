//------------------------------------------------------------------
// 세그먼트형 BM25 점수 동일성 시험 (인덱싱 속도 개선 설계서 §6 — "BM25 C 점수 동일")
//=> 덧붙임 · 지움으로 고쳐 온 BM25 와, 같은 저장소로 처음부터 다시 만든 BM25 의 검색 결과를 견준다.
//    1) 벡터 저장소(sqlite)를 메모리로 올리고 토큰 표로 BM25 를 새로 만든다(임시 파일)
//    2) 질의를 모은다 — 질의 파일(한 줄 하나)이 있으면 그것, 없으면 청크 본문 앞부분을 무작위로 뽑는다
//    3) 질의마다 두 색인의 상위 50 을 견준다 — 순서가 같아야 하고 점수 차가 1e-5 이하여야 한다
//    4) 질의 시간도 두 쪽을 따로 잰다(목표: 세그먼트형이 +10ms 이내)
//
// 실행: cargo run --release --example bm25_verify -- <rag_index 폴더> <고쳐 온 bm25 파일> [질의 수] [질의 파일]
//------------------------------------------------------------------

use std::time::Instant;

use simplerag::{bm25::{self, Bm25Index}, embed::Embedder, store::VectorStore};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n_q: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(500);
    let store = VectorStore::new(Some(args[1].clone()));
    store.ensure_loaded().expect("저장소 열기 실패");
    let emb = Embedder::new();
    let tok = emb.tokenizer().unwrap();

    // 고쳐 온 색인(세그먼트형)
    let seg = Bm25Index::new(Some(args[2].clone()));
    assert!(seg.ensure_loaded() >= 0.0, "고쳐 온 BM25 를 읽지 못했습니다");
    // 같은 저장소로 처음부터
    let ids = store.ids().unwrap();
    let tfs = store.tf_of(None).unwrap();
    let texts = store.texts_of(&ids).unwrap();
    let all: Vec<bm25::Tf> = ids.iter().zip(&texts)
        .map(|(i, t)| tfs.get(i).and_then(|b| bm25::tf_from_blob(b)).unwrap_or_else(|| tok.term_freqs(t))).collect();
    let tmp = std::env::temp_dir().join(format!("bm25_verify_{}.bin", std::process::id()));
    let full = Bm25Index::new(Some(tmp.to_string_lossy().into_owned()));
    full.build_tf(&ids, &all).unwrap();
    println!("청크 {} · 고쳐 온 색인 (본, 덧붙임, 지움) = {:?}", ids.len(), seg.counts());

    // 질의 — 파일이 있으면 그것, 없으면 본문 앞 6~30글자를 고르게 뽑는다
    let queries: Vec<String> = match args.get(4) {
        Some(p) => std::fs::read_to_string(p).unwrap().lines().map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).take(n_q).collect(),
        None => (0..n_q).map(|k| {
            let t = &texts[(k * 7919) % texts.len()];
            let skip = (k * 13) % 40;
            t.chars().skip(skip).take(6 + k % 25).collect()
        }).collect(),
    };
    let (mut bad_order, mut max_diff, mut t_seg, mut t_full) = (0usize, 0f32, 0f64, 0f64);
    for q in &queries {
        let t = Instant::now();
        let a = seg.search_scored(q, &tok, 50, None);
        t_seg += t.elapsed().as_secs_f64();
        let t = Instant::now();
        let b = full.search_scored(q, &tok, 50, None);
        t_full += t.elapsed().as_secs_f64();
        if a.iter().map(|x| x.0).collect::<Vec<_>>() != b.iter().map(|x| x.0).collect::<Vec<_>>() {
            bad_order += 1;
            if bad_order <= 3 {
                println!("  순서 다름: {:?}\n    고쳐 온 {:?}\n    처음부터 {:?}", q, &a[..a.len().min(5)], &b[..b.len().min(5)]);
            }
        }
        for (x, y) in a.iter().zip(&b) {
            max_diff = max_diff.max((x.1 - y.1).abs());
        }
    }
    let _ = std::fs::remove_file(&tmp);
    let n = queries.len().max(1) as f64;
    println!("질의 {} · 상위 50 순서 다름 {} · 최대 점수 차 {:.2e}", queries.len(), bad_order, max_diff);
    println!("질의 평균 — 고쳐 온 {:.2}ms · 처음부터 {:.2}ms", t_seg / n * 1000.0, t_full / n * 1000.0);
    println!("판정: {}", if bad_order == 0 && max_diff <= 1e-5 { "통과" } else { "실패" });
}
