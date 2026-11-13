//------------------------------------------------------------------
// 골든 질문 검색만 돌리기 — 근거 3개(실제 앱과 같은 검색 · RRF · 리랭킹)를 줄마다 남긴다 (진단용 예제)
//=> 메모리 벡터 f16(B안)이 최종 근거를 바꾸는지 보려고, 같은 질문을 SIMPLERAG_VECTOR_F16=1 · 0 으로
//   두 번 돌려 결과 파일을 맞댄다. 답변 모델은 올리지 않는다(검색만).
//
// 실행: cargo run --release --example golden_retrieve -- <질문 파일> <결과 파일(jsonl)>
//------------------------------------------------------------------

use std::io::Write;
use std::time::Instant;

use simplerag::app::App;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let qs: Vec<String> = std::fs::read_to_string(&a[1]).unwrap().lines().filter(|l| !l.trim().is_empty()).map(|s| s.to_string()).collect();
    let app = App::new(None);
    // 생성 모델 없이 — 리랭커는 앱과 같은 규칙(iGPU 백엔드면 켬)
    app.warmup(true, &mut |_| {}, None);
    app.wait_rerank();
    let mut out = std::fs::File::create(&a[2]).unwrap();
    let t = Instant::now();
    for (i, q) in qs.iter().enumerate() {
        let (chunks, _) = app.retriever.search(q, None, None).unwrap();
        let line = serde_json::json!({
            "i": i,
            "ids": chunks.iter().map(|c| c.id).collect::<Vec<_>>(),
            "docs": chunks.iter().map(|c| c.doc_path.clone()).collect::<Vec<_>>(),
        });
        writeln!(out, "{}", line).unwrap();
    }
    eprintln!("질문 {} 개 · 질문당 {:.0} ms · 리랭커 {}", qs.len(), t.elapsed().as_secs_f64() / qs.len() as f64 * 1000.0,
        app.retriever.reranker.lock().unwrap().is_some());
}
