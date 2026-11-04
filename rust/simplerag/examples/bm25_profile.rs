//------------------------------------------------------------------
// BM25 재구축 시간 측정 (진단용 예제)
//=> index 가 끝날 때마다 하는 "저장소 전체 읽기 → BM25 다시 만들기" 를 따로 잰다.
//   문서 1건만 바뀌어도 이 비용을 통째로 내므로, 큰 인덱스에서 얼마인지 본다.
//    1) 벡터 저장소(sqlite)를 메모리로 올린다
//    2) 모든 청크 본문을 모은다
//    3) 토큰화만 따로 잰다 — 한 줄씩(지금 방식) / 스레드 N개로 나눠서(병렬안)
//    4) BM25 를 만들어 지정 파일에 저장한다(토큰화 포함 전체)
//
// 실행: cargo run --release --example bm25_profile -- <rag_index 폴더> <bm25 출력 파일> [스레드 수]
//------------------------------------------------------------------

use std::time::Instant;

use simplerag::{bm25::Bm25Index, embed::Embedder, store::VectorStore};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let threads: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(8);
    let store = VectorStore::new(Some(args[1].clone()));
    let t = Instant::now();
    store.ensure_loaded().expect("저장소 열기 실패");
    let t_load = t.elapsed().as_secs_f64();
    let t = Instant::now();
    let pts = store.scroll_all().unwrap();
    let texts: Vec<String> = pts.iter().map(|p| p.1.clone()).collect();
    let ids: Vec<i64> = pts.iter().map(|p| p.0).collect();
    let t_scroll = t.elapsed().as_secs_f64();
    let emb = Embedder::new();
    let tok = emb.tokenizer().unwrap();

    // 토큰화만 — 한 줄씩(build 안과 같은 방식)
    let t = Instant::now();
    let n1: usize = texts.iter().map(|x| tok.tokens(x).len()).sum();
    let t_tok_seq = t.elapsed().as_secs_f64();
    // 토큰화만 — 청크를 스레드 수만큼 나눠 동시에
    let t = Instant::now();
    let per = (texts.len() + threads - 1) / threads;
    let n2: usize = std::thread::scope(|s| {
        let hs: Vec<_> = texts.chunks(per.max(1)).map(|part| {
            let tok = &tok;
            s.spawn(move || part.iter().map(|x| tok.tokens(x).len()).sum::<usize>())
        }).collect();
        hs.into_iter().map(|h| h.join().unwrap()).sum()
    });
    let t_tok_par = t.elapsed().as_secs_f64();
    assert_eq!(n1, n2);

    let bm = Bm25Index::new(Some(args[2].clone()));
    let t = Instant::now();
    bm.build(&texts, &ids, &tok).expect("BM25 실패");
    let t_build = t.elapsed().as_secs_f64();
    println!("청크 {} · 토큰 {} | 저장소 적재 {:.1}s · 본문 모으기 {:.1}s", texts.len(), n1, t_load, t_scroll);
    println!("토큰화만: 한 줄씩 {:.1}s · {}스레드 {:.1}s", t_tok_seq, threads, t_tok_par);
    println!("BM25 구축 전체(토큰화+역색인+저장) {:.1}s → 토큰화 뺀 나머지 약 {:.1}s", t_build, t_build - t_tok_seq);
}
