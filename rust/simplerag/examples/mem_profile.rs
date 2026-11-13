//------------------------------------------------------------------
// chat 워커 메모리 나눠 재기 (진단용 예제)
//=> chat 은 구성요소를 동시에 올려 어느 것이 얼마를 먹는지 안 보인다.
//   여기서는 하나씩 차례로 올리고, 매 단계 뒤 이 프로세스의
//   전용 메모리(commit) · 실제 램(working set) · 공유 GPU 메모리를 적는다.
//    1) 시작 직후(기준)
//    2) 임베딩 → 벡터 저장소 → BM25 → 리랭커 → 생성 모델(적재·예열)
//    3) 실제 질문 1건 답변 뒤
//
// 실행: SIMPLERAG_HOME 등을 설치 폴더로 맞춘 뒤
//       cargo run --release --example mem_profile -- "<질문>"
//------------------------------------------------------------------

use std::process::Command;
use std::time::Instant;

use simplerag::app::{App, Event};
use simplerag::generate::backend;

// 이 프로세스 메모리 한 줄 — PowerShell 로 Private · WS · GPU 공유를 MB 로 읽는다
fn mem() -> (f64, f64, f64) {
    let pid = std::process::id();
    let ps = format!(
        "$p=Get-Process -Id {pid}; $g=(Get-Counter '\\GPU Process Memory(pid_{pid}*)\\Shared Usage' -EA SilentlyContinue).CounterSamples | Measure-Object CookedValue -Sum; \
         '{{0}} {{1}} {{2}}' -f [int]($p.PrivateMemorySize64/1MB), [int]($p.WorkingSet64/1MB), [int]($g.Sum/1MB)"
    );
    let out = Command::new("powershell").args(["-NoProfile", "-Command", &ps]).output().expect("powershell");
    let s = String::from_utf8_lossy(&out.stdout);
    let v: Vec<f64> = s.split_whitespace().filter_map(|x| x.parse().ok()).collect();
    (v.first().copied().unwrap_or(0.0), v.get(1).copied().unwrap_or(0.0), v.get(2).copied().unwrap_or(0.0))
}

fn main() {
    let q = std::env::args().nth(1).unwrap_or_else(|| "연차 휴가는 며칠인가요?".into());
    let mut prev = mem();
    println!("{:<22} {:>9} {:>9} {:>9} | {:>8} {:>8} {:>8}", "단계", "전용MB", "램WS MB", "GPU공유", "+전용", "+램", "+GPU");
    // 단계 하나 기록 — 직전 단계 대비 늘어난 양을 함께
    let mut row = |name: &str, extra: String| {
        let m = mem();
        println!("{:<22} {:>9.0} {:>9.0} {:>9.0} | {:>+8.0} {:>+8.0} {:>+8.0}  {}", name, m.0, m.1, m.2, m.0 - prev.0, m.1 - prev.1, m.2 - prev.2, extra);
        prev = m;
    };
    row("시작", String::new());

    let app = App::new(None);
    let t = Instant::now();
    let r = app.embedder.ensure_loaded();
    row("임베딩(e5-small)", format!("{:.1}s {:?}", t.elapsed().as_secs_f64(), r.err()));

    let t = Instant::now();
    let r = app.store.ensure_loaded();
    let n = app.store.stats().map(|s| s.0).unwrap_or(0);
    row("벡터 저장소", format!("{:.1}s 청크 {} {:?}", t.elapsed().as_secs_f64(), n, r.err()));

    let t = Instant::now();
    app.bm25.ensure_loaded();
    row("BM25", format!("{:.1}s", t.elapsed().as_secs_f64()));

    let t = Instant::now();
    let r = app.reranker.ensure_loaded();
    *app.retriever.reranker.lock().unwrap() = if r.is_ok() { Some(app.reranker.clone()) } else { None };
    row("리랭커(bge int8)", format!("{:.1}s {:?}", t.elapsed().as_secs_f64(), r.err()));

    // 생성 백엔드는 chat 과 같이 캐시(gen_backend.json)에서 고른다
    let info = backend::select(Some(&app.generator.alias), false, &mut |m| eprintln!("{}", m));
    let b = info["backend"].as_str().unwrap_or("cpu").to_string();
    *app.generator.backend.lock().unwrap() = b.clone();
    let t = Instant::now();
    let r = app.generator.ensure_loaded();
    row("생성 모델 적재", format!("{:.1}s {} {:?}", t.elapsed().as_secs_f64(), b, r.err()));
    let t = Instant::now();
    let r = app.generator.warm();
    row("생성 모델 예열", format!("{:.1}s {:?}", t.elapsed().as_secs_f64(), r.err()));

    let t = Instant::now();
    let mut toks = 0usize;
    let mut first: Option<f64> = None;
    let r = app.answer(&q, None, None, None, None, None, false, &mut |e| {
        if let Event::Token(_) = e {
            toks += 1;
            first.get_or_insert(t.elapsed().as_secs_f64());
        }
    });
    row("질문 1건 답변", format!("{:.1}s 첫글자 {:.1}s 토큰 {} {:?}", t.elapsed().as_secs_f64(), first.unwrap_or(0.0), toks, r.err()));
}
