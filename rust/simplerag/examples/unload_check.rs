//------------------------------------------------------------------
// 생성 모델 · 리랭커 내렸다 올리기 — 메모리가 실제로 돌아오나 (진단용 예제)
//=> 쉬는 동안 두 모델을 내리는 설계를 검토하려고, chat 워커와 같은 상태(임베딩·저장소·BM25 상주)에서
//   리랭커와 생성 모델을 올림 → 한 번 씀 → 내림 을 세 번 되풀이하며 매번 메모리와 다시 올리는 시간을 잰다.
//   세 번째까지 내린 뒤 값이 처음 기준으로 돌아오면 새는 것 없이 반환되는 것이다.
//
// 실행: SIMPLERAG_HOME 등을 설치 폴더로 맞춘 뒤
//       cargo run --release --example unload_check -- [되풀이 수=3]
//------------------------------------------------------------------

use std::process::Command;
use std::time::Instant;

use simplerag::app::App;
use simplerag::embed::Reranker;
use simplerag::generate::backend;
use simplerag::generate::generator::Generator;

// 이 프로세스 메모리 — (전용 MB, 램 WS MB, 공유 GPU MB)
fn mem() -> (f64, f64, f64) {
    let pid = std::process::id();
    let ps = format!(
        "$p=Get-Process -Id {pid}; $g=(Get-Counter '\\GPU Process Memory(pid_{pid}*)\\Shared Usage' -EA SilentlyContinue).CounterSamples | Measure-Object CookedValue -Sum; \
         '{{0}} {{1}} {{2}}' -f [int]($p.PrivateMemorySize64/1MB), [int]($p.WorkingSet64/1MB), [int]($g.Sum/1MB)"
    );
    let out = Command::new("powershell").args(["-NoProfile", "-Command", &ps]).output().expect("powershell");
    let v: Vec<f64> = String::from_utf8_lossy(&out.stdout).split_whitespace().filter_map(|x| x.parse().ok()).collect();
    (v.first().copied().unwrap_or(0.0), v.get(1).copied().unwrap_or(0.0), v.get(2).copied().unwrap_or(0.0))
}

// 힙 정리 — 비어 있는 힙 덩어리를 OS 에 돌려준다(내린 뒤 남는 몫이 줄어드나 보려고)
#[link(name = "kernel32")]
extern "system" {
    fn GetProcessHeaps(n: u32, heaps: *mut isize) -> u32;
    fn HeapCompact(heap: isize, flags: u32) -> usize;
}

// 모든 힙을 HeapCompact — 환경변수 TRIM=1 일 때만
fn trim_heaps() {
    if std::env::var("TRIM").as_deref() != Ok("1") {
        return;
    }
    unsafe {
        let mut hs = vec![0isize; 256];
        let n = GetProcessHeaps(256, hs.as_mut_ptr()) as usize;
        for h in &hs[..n.min(256)] {
            HeapCompact(*h, 0);
        }
    }
}

fn main() {
    let rounds: usize = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(3);
    // 무엇을 되풀이할지 — both | rr(리랭커만) | gen(생성 모델만) : 남는 몫이 어디서 나오는지 나눠 보려고
    let what = std::env::args().nth(2).unwrap_or_else(|| "both".into());
    let (do_rr, do_gen) = (what != "gen", what != "rr");
    // 상주 몫(임베딩·저장소·BM25) — chat 워커와 같은 바탕
    let app = App::new(None);
    app.embedder.ensure_loaded().unwrap();
    app.store.ensure_loaded().unwrap();
    app.bm25.ensure_loaded();
    let info = backend::select(Some(&app.generator.alias), false, &mut |m| eprintln!("{}", m));
    let be = info["backend"].as_str().unwrap_or("cpu").to_string();
    let base = mem();
    println!("상주만(임베딩·저장소·BM25): 전용 {:.0} · WS {:.0} · GPU {:.0} MB  [생성 백엔드 {}]", base.0, base.1, base.2, be);
    let passages: Vec<String> = (0..10).map(|i| format!("연차 유급휴가는 1년간 80퍼센트 이상 출근한 근로자에게 15일을 준다. 항목 {}", i)).collect();
    for r in 1..=rounds {
        // 리랭커 올리고 한 번 쓰기
        let rr = Reranker::new();
        let (mut t_rr, mut t_rs) = (0.0, 0.0);
        if do_rr {
            let t = Instant::now();
            rr.ensure_loaded().unwrap();
            t_rr = t.elapsed().as_secs_f64();
            let t = Instant::now();
            rr.score("연차 휴가는 며칠인가요?", &passages).unwrap();
            t_rs = t.elapsed().as_secs_f64();
        }
        // 생성 모델 올리고 예열 · 짧은 답 하나
        let g = Generator::new(None);
        *g.backend.lock().unwrap() = be.clone();
        let (mut t_gl, mut t_gw, mut ans) = (0.0, 0.0, String::new());
        if do_gen {
            let t = Instant::now();
            g.ensure_loaded().unwrap();
            t_gl = t.elapsed().as_secs_f64();
            let t = Instant::now();
            g.warm().unwrap();
            t_gw = t.elapsed().as_secs_f64();
            ans = g.generate("연차 휴가는 며칠인가요?", &passages[..3], Some(48)).unwrap_or_default();
        }
        let up = mem();
        // 내리기 — 객체를 버리면 세션·모델·컨텍스트가 풀린다
        drop(rr);
        drop(g);
        trim_heaps();
        std::thread::sleep(std::time::Duration::from_millis(500));
        let down = mem();
        println!("[{}회] 올림: 리랭커 {:.2}s(첫 점수 {:.2}s) · 생성 모델 {:.2}s + 예열 {:.2}s · 답 {}자", r, t_rr, t_rs, t_gl, t_gw, ans.chars().count());
        println!("      올린 뒤 전용 {:.0} (+{:.0}) · GPU {:.0} (+{:.0}) MB → 내린 뒤 전용 {:.0} (기준 대비 {:+.0}) · GPU {:.0} ({:+.0}) MB",
            up.0, up.0 - base.0, up.2, up.2 - base.2, down.0, down.0 - base.0, down.2, down.2 - base.2);
    }
}
