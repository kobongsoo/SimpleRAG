//------------------------------------------------------------------
// ONNX 최적화 결과 저장 → 적재 시간 비교 (진단용 예제)
//=> 리랭커 적재가 4초 걸린다. 그중 그래프 최적화(Level3) 몫이 얼마인지, 최적화가 끝난 모델을
//   파일로 저장해 두고 다음부터 최적화 없이 읽으면 얼마나 빨라지는지, 점수는 그대로인지 잰다.
//    1) 토크나이저 읽기 시간(따로)
//    2) 지금 방식 — 원본 + Level3 (3번)
//    3) 원본 + Level1 (3번) — 저장 파일 없이 고를 수 있는 길
//    4) 최적화 결과 저장(한 번 드는 비용) → 저장본 + 최적화 끔 (3번)
//    5) 같은 입력으로 2) · 4) 출력 차이
//
// 실행: cargo run --release --example onnx_cache -- <모델 폴더> <onnx 파일 이름> <저장할 폴더> <스레드> <inter1:0|1>
//------------------------------------------------------------------

use std::time::Instant;

use ort::session::{builder::GraphOptimizationLevel, Session, SessionInputValue};
use ort::value::Tensor;

// 세션 하나 만들기 — 최적화 수준·저장 경로를 골라서
fn build(path: &str, lvl: GraphOptimizationLevel, threads: usize, inter_one: bool, save: Option<&str>) -> Session {
    let mut b = Session::builder().unwrap().with_optimization_level(lvl).unwrap().with_intra_threads(threads).unwrap();
    if inter_one {
        b = b.with_inter_threads(1).unwrap();
    }
    if let Some(s) = save {
        b = b.with_optimized_model_path(s).unwrap();
    }
    b.commit_from_file(path).unwrap()
}

// 같은 시험 입력으로 첫 출력 — 입력 이름으로 값을 고른다(mask=1, token_type=0, 나머지=토큰 id)
fn run(s: &mut Session) -> Vec<f32> {
    let w = 48usize;
    let ids: Vec<i64> = (0..w as i64).map(|i| if i == 0 { 0 } else if i == w as i64 - 1 { 2 } else { 1000 + i * 37 }).collect();
    let names: Vec<String> = s.inputs.iter().map(|i| i.name.clone()).collect();
    let feeds: Vec<(String, SessionInputValue)> = names.iter().map(|n| {
        let v: Vec<i64> = if n.contains("mask") { vec![1; w] } else if n.contains("type") { vec![0; w] } else { ids.clone() };
        (n.clone(), Tensor::from_array(([1usize, w], v)).unwrap().into())
    }).collect();
    let out = s.run(feeds).unwrap();
    let (_, d) = out[0].try_extract_tensor::<f32>().unwrap();
    d.to_vec()
}

// 세 번 재서 각 시간(초)
fn times(f: impl Fn() -> Session) -> Vec<f64> {
    (0..3).map(|_| {
        let t = Instant::now();
        let s = f();
        let e = t.elapsed().as_secs_f64();
        drop(s);
        e
    }).collect()
}

fn main() {
    simplerag::embed::ensure_dylib();
    let a: Vec<String> = std::env::args().collect();
    let (dir, file, out_dir) = (&a[1], &a[2], &a[3]);
    let threads: usize = a[4].parse().unwrap();
    let inter_one = a[5] == "1";
    let path = format!("{}\\{}", dir, file);
    let saved = format!("{}\\{}.opt.onnx", out_dir, file);

    let t = Instant::now();
    let _tok = tokenizers::Tokenizer::from_file(format!("{}\\tokenizer.json", dir)).unwrap();
    println!("토크나이저 읽기 {:.2}s", t.elapsed().as_secs_f64());

    let l3 = times(|| build(&path, GraphOptimizationLevel::Level3, threads, inter_one, None));
    println!("원본 + Level3 (지금) {:?}", l3.iter().map(|x| format!("{:.2}s", x)).collect::<Vec<_>>());
    let l1 = times(|| build(&path, GraphOptimizationLevel::Level1, threads, inter_one, None));
    println!("원본 + Level1        {:?}", l1.iter().map(|x| format!("{:.2}s", x)).collect::<Vec<_>>());

    let t = Instant::now();
    drop(build(&path, GraphOptimizationLevel::Level3, threads, inter_one, Some(&saved)));
    let sz = std::fs::metadata(&saved).map(|m| m.len()).unwrap_or(0);
    println!("최적화 결과 저장(한 번) {:.2}s · 원본 {:.0} MB → 저장본 {:.0} MB", t.elapsed().as_secs_f64(),
        std::fs::metadata(&path).unwrap().len() as f64 / 1048576.0, sz as f64 / 1048576.0);
    let dis = times(|| build(&saved, GraphOptimizationLevel::Disable, threads, inter_one, None));
    println!("저장본 + 최적화 끔    {:?}", dis.iter().map(|x| format!("{:.2}s", x)).collect::<Vec<_>>());

    // 출력 비교 — 지금 방식 vs 저장본 vs Level1
    let o3 = run(&mut build(&path, GraphOptimizationLevel::Level3, threads, inter_one, None));
    let os = run(&mut build(&saved, GraphOptimizationLevel::Disable, threads, inter_one, None));
    let o1 = run(&mut build(&path, GraphOptimizationLevel::Level1, threads, inter_one, None));
    let diff = |x: &[f32], y: &[f32]| x.iter().zip(y).map(|(a, b)| (a - b).abs()).fold(0f32, f32::max);
    println!("출력 칸 {} · 최대 차이: 저장본 {:.2e} · Level1 {:.2e}", o3.len(), diff(&o3, &os), diff(&o3, &o1));

    // 추론 속도도 — 같은 입력 20번
    for (name, mut s) in [("Level3", build(&path, GraphOptimizationLevel::Level3, threads, inter_one, None)),
                          ("저장본", build(&saved, GraphOptimizationLevel::Disable, threads, inter_one, None)),
                          ("Level1", build(&path, GraphOptimizationLevel::Level1, threads, inter_one, None))] {
        run(&mut s);
        let t = Instant::now();
        for _ in 0..20 {
            run(&mut s);
        }
        println!("추론 1회 ({}) {:.1} ms", name, t.elapsed().as_secs_f64() * 1000.0 / 20.0);
    }
}
