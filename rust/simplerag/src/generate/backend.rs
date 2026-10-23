//------------------------------------------------------------------
// 생성 백엔드 고르기 — CPU 또는 iGPU(Vulkan) (Python generate/backend.py)
//=> PC 마다 iGPU 유무·성능이 달라 처음 한 번 재서 고른다. 결과는 gen_backend.json 에 남긴다.
//    · 재기는 별도 자식 프로세스(`simplerag-rs _probe-backend`)에서 — 한 프로세스는 DLL 을 한 번만
//      고를 수 있고, 드라이버 문제로 죽어도 본 프로세스는 살아야 하기 때문이다
//    · iGPU prefill 이 CPU 의 1.3배 이상이면 iGPU
//    · 리랭킹은 auto 면 iGPU 일 때만 켠다
//------------------------------------------------------------------

use std::sync::Mutex;
use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::config::{self, cfg};
use crate::pyutil;

pub const VULKAN_DLLS: &[&str] = &["ggml-base.dll", "ggml-cpu.dll", "ggml-vulkan.dll", "ggml.dll", "llama.dll"];
const CACHE_KEEP: usize = 4;

static SELECTED: Mutex<Option<Value>> = Mutex::new(None);

pub fn label(backend: &str) -> &'static str {
    if backend == "vulkan" { "iGPU(Vulkan)" } else { "CPU" }
}

//------------------------------------------------------------------
// 리랭킹을 켤지 — 1/on 강제 켬, 0/off 강제 끔, auto 는 iGPU 일 때만
//------------------------------------------------------------------
pub fn rerank_enabled(backend: &str) -> bool {
    match cfg().rerank.as_str() {
        "1" | "on" | "true" | "yes" => true,
        "0" | "off" | "false" | "no" => false,
        _ => backend == "vulkan",
    }
}

// 동봉 DLL 의 llama-cpp-python 버전(VERSION.txt)
fn read_version(dir: &str) -> Option<String> {
    std::fs::read_to_string(pyutil::join(dir, "VERSION.txt")).ok().map(|s| pyutil::strip(&s).to_string()).filter(|s| !s.is_empty())
}

//------------------------------------------------------------------
// Vulkan 판을 쓸 수 있는가 — (ok, 사유)
//------------------------------------------------------------------
pub fn vulkan_available() -> (bool, String) {
    let dir = &cfg().vulkan_lib_dir;
    if !std::path::Path::new(dir).is_dir() {
        return (false, format!("Vulkan 런타임 폴더 없음: {}", dir));
    }
    let missing: Vec<&str> = VULKAN_DLLS.iter().copied().filter(|f| !std::path::Path::new(&pyutil::join(dir, f)).is_file()).collect();
    if !missing.is_empty() {
        return (false, format!("Vulkan DLL 누락: {}", missing.join(", ")));
    }
    let (want, have) = (read_version(dir), read_version(&cfg().cpu_lib_dir));
    if let (Some(w), Some(h)) = (&want, &have) {
        if w != h {
            return (false, format!("Vulkan DLL 버전({}) ≠ llama-cpp-python({})", w, h));
        }
    }
    let sysroot = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".into());
    if !std::path::Path::new(&pyutil::join(&pyutil::join(&sysroot, "System32"), "vulkan-1.dll")).is_file() {
        return (false, "Vulkan 런타임(vulkan-1.dll) 없음 — 그래픽 드라이버가 없거나 오래됨".into());
    }
    (true, String::new())
}

//------------------------------------------------------------------
// 두 측정으로 고르기 — (백엔드, 사유)
//------------------------------------------------------------------
pub fn decide(cpu: &Value, gpu: &Value) -> (String, String) {
    let ok = |v: &Value| v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false);
    let tps = |v: &Value| v.get("prefill_tps").and_then(|x| x.as_f64()).unwrap_or(0.0);
    if !ok(gpu) {
        return ("cpu".into(), format!("iGPU 사용 불가 — {}", gpu.get("error").and_then(|x| x.as_str()).unwrap_or("측정 안 됨")));
    }
    if !ok(cpu) {
        return ("vulkan".into(), format!("CPU 측정 실패 — iGPU 사용 ({})", cpu.get("error").and_then(|x| x.as_str()).unwrap_or("")));
    }
    let ratio = tps(gpu) / tps(cpu).max(1e-6);
    if ratio >= config::GPU_MIN_SPEEDUP {
        return ("vulkan".into(), format!("iGPU prefill {:.0} t/s = CPU {:.0} t/s 의 {:.1}배", tps(gpu), tps(cpu), ratio));
    }
    ("cpu".into(), format!("iGPU 가 충분히 빠르지 않음 ({:.0} vs {:.0} t/s, {:.1}배 < {:.1}배)", tps(gpu), tps(cpu), ratio, config::GPU_MIN_SPEEDUP))
}

// 컴퓨터 이름(platform.node())
fn host() -> String {
    std::env::var("COMPUTERNAME").unwrap_or_default()
}

//------------------------------------------------------------------
// 측정 저장 키 — 이 값이 같아야 저장된 측정을 다시 쓴다(Python 판과 같은 모양)
//------------------------------------------------------------------
pub fn cache_key(alias: Option<&str>) -> Value {
    let alias = alias.map(|s| s.to_string()).unwrap_or_else(|| cfg().default_gen_model.clone());
    let dll = std::fs::metadata(pyutil::join(&cfg().vulkan_lib_dir, "ggml-vulkan.dll")).ok().map(|m| {
        let mt = m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0);
        format!("{}:{}", m.len(), mt)
    });
    json!({
        "host": host(),
        "model": config::gen_file(&alias).unwrap_or(&alias),
        "llama_cpp_python": read_version(&cfg().vulkan_lib_dir).or_else(|| read_version(&cfg().cpu_lib_dir)),
        "vulkan_dll": dll,
        "min_speedup": config::GPU_MIN_SPEEDUP,
        "threads": config::GEN_THREADS,
    })
}

fn load_entries() -> Vec<Value> {
    let raw = match std::fs::read(&cfg().backend_cache_path) {
        Ok(b) => b,
        Err(_) => return vec![],
    };
    let s = String::from_utf8_lossy(&raw);
    match serde_json::from_str::<Value>(s.trim_start_matches('\u{feff}')) {
        Ok(Value::Object(m)) => {
            if let Some(Value::Array(a)) = m.get("entries") {
                a.iter().filter(|e| e.is_object()).cloned().collect()
            } else if m.contains_key("key") {
                vec![Value::Object(m)]
            } else {
                vec![]
            }
        }
        _ => vec![],
    }
}

// 키 비교(숫자 1.3 과 1.3 등)
fn key_eq(a: &Value, b: &Value) -> bool {
    match (a.as_object(), b.as_object()) {
        (Some(x), Some(y)) => x.len() == y.len() && x.iter().all(|(k, v)| match (v, y.get(k)) {
            (Value::Number(p), Some(Value::Number(q))) => p.as_f64() == q.as_f64(),
            (p, q) => Some(p) == q,
        }),
        _ => false,
    }
}

//------------------------------------------------------------------
// 저장된 측정 보기(재지 않는다)
//------------------------------------------------------------------
pub fn peek(alias: Option<&str>) -> Option<Value> {
    let want = cache_key(alias);
    load_entries().into_iter().find(|e| e.get("key").map(|k| key_eq(k, &want)).unwrap_or(false))
}

fn save(data: &Value) {
    let mut entries: Vec<Value> = load_entries().into_iter()
        .filter(|e| !e.get("key").map(|k| key_eq(k, &data["key"])).unwrap_or(false)).collect();
    entries.insert(0, data.clone());
    entries.truncate(CACHE_KEEP);
    let payload = json!({"version": 2, "entries": entries});
    if let Ok(s) = serde_json::to_string_pretty(&payload) {
        let _ = pyutil::write_atomic(&cfg().backend_cache_path, s.as_bytes());
    }
}

//------------------------------------------------------------------
// 자식 프로세스로 한 번 재기
//------------------------------------------------------------------
pub fn run_probe(kind: &str, model_path: &str) -> Value {
    let t0 = Instant::now();
    let exe = match std::env::current_exe() {
        Ok(e) => e,
        Err(e) => return json!({"ok": false, "error": format!("측정 프로세스 실행 실패: {}", e)}),
    };
    let mut cmd = std::process::Command::new(exe);
    cmd.args(["_probe-backend", kind, model_path]).stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped());
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return json!({"ok": false, "error": format!("측정 프로세스 실행 실패: {}", e)}),
    };
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });
    let out = match rx.recv_timeout(std::time::Duration::from_secs(config::BACKEND_PROBE_TIMEOUT)) {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return json!({"ok": false, "error": format!("측정 프로세스 실행 실패: {}", e)}),
        Err(_) => return json!({"ok": false, "error": format!("시간 초과({}초)", config::BACKEND_PROBE_TIMEOUT)}),
    };
    let stdout = String::from_utf8_lossy(&out.stdout);
    let mut found: Option<Value> = None;
    for line in stdout.lines().rev() {
        let l = pyutil::strip(line);
        if l.starts_with('{') {
            if let Ok(v) = serde_json::from_str::<Value>(l) {
                found = Some(v);
                break;
            }
        }
    }
    match found {
        Some(mut v) => {
            v["wall_s"] = pyutil::jf(pyutil::round_to(t0.elapsed().as_secs_f64(), 1));
            v
        }
        None => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            let tail = stderr.lines().filter(|l| !pyutil::strip(l).is_empty()).last().unwrap_or("");
            json!({"ok": false, "error": format!("측정 프로세스 비정상 종료(exit={}) {}", out.status.code().unwrap_or(-1), pyutil::head(tail, 160))})
        }
    }
}

//------------------------------------------------------------------
// 백엔드 고르기 (한 프로세스에 한 번)
//------------------------------------------------------------------
pub fn select(alias: Option<&str>, reprobe: bool, log: &mut dyn FnMut(&str)) -> Value {
    let mut g = SELECTED.lock().unwrap();
    if let Some(v) = g.as_ref() {
        if !reprobe {
            return v.clone();
        }
    }
    let info = select_inner(alias, reprobe, log);
    *g = Some(info.clone());
    info
}

fn select_inner(alias: Option<&str>, reprobe: bool, log: &mut dyn FnMut(&str)) -> Value {
    if let Some(api) = super::llama::loaded() {
        let gpu = api.supports_gpu_offload();
        return json!({"backend": if gpu { "vulkan" } else { "cpu" }, "source": "loaded",
                      "reason": "llama.dll 이 이미 적재됨 — 적재된 DLL 을 그대로 쓴다"});
    }
    let mut mode = cfg().gen_backend.clone();
    if mode == "cpu" {
        return json!({"backend": "cpu", "source": "config", "reason": "SIMPLERAG_GEN_BACKEND=cpu"});
    }
    if mode != "auto" && mode != "vulkan" {
        log(&format!("[backend] 알 수 없는 SIMPLERAG_GEN_BACKEND={} — auto 로 처리", mode));
        mode = "auto".into();
    }
    let (ok, why) = vulkan_available();
    if !ok {
        return json!({"backend": "cpu", "source": "check", "reason": why});
    }
    let cached = if reprobe { None } else { peek(alias) };
    if let Some(c) = &cached {
        if mode == "auto" {
            return json!({"backend": c["backend"], "source": "cache", "reason": c.get("reason").cloned().unwrap_or(json!("")),
                          "cpu": c.get("cpu"), "vulkan": c.get("vulkan")});
        }
        if c.get("vulkan").and_then(|v| v.get("ok")).and_then(|x| x.as_bool()).unwrap_or(false) {
            return json!({"backend": "vulkan", "source": "cache", "reason": "SIMPLERAG_GEN_BACKEND=vulkan", "vulkan": c.get("vulkan")});
        }
    }
    let alias_s = alias.map(|s| s.to_string()).unwrap_or_else(|| cfg().default_gen_model.clone());
    let model_path = config::gen_model_path(&alias_s);
    if !std::path::Path::new(&model_path).is_file() {
        return json!({"backend": "cpu", "source": "check", "reason": "생성 모델 없음 — 측정 생략"});
    }
    log("[backend] 이 PC 의 CPU/iGPU 생성 속도를 잽니다 (처음 한 번, 약 15~40초)");
    let gpu = run_probe("vulkan", &model_path);
    if mode == "vulkan" {
        if gpu.get("ok").and_then(|x| x.as_bool()).unwrap_or(false) {
            return json!({"backend": "vulkan", "source": "probe", "reason": "SIMPLERAG_GEN_BACKEND=vulkan", "vulkan": gpu});
        }
        return json!({"backend": "cpu", "source": "probe",
                      "reason": format!("iGPU 지정됐지만 사용 불가 — {}", gpu.get("error").and_then(|x| x.as_str()).unwrap_or("")), "vulkan": gpu});
    }
    let cpu = run_probe("cpu", &model_path);
    let (backend, reason) = decide(&cpu, &gpu);
    save(&json!({"key": cache_key(alias), "backend": backend, "reason": reason, "cpu": cpu, "vulkan": gpu,
                 "probed_at": chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()}));
    log(&format!("[backend] {} 선택 — {}", label(&backend), reason));
    json!({"backend": backend, "source": "probe", "reason": reason, "cpu": cpu, "vulkan": gpu})
}

//------------------------------------------------------------------
// 자식 프로세스 본체 — 한 백엔드로 적재해 prefill 속도를 재고 JSON 한 줄을 찍는다
//------------------------------------------------------------------
pub fn probe_main(args: &[String]) -> i32 {
    let kind = args.first().cloned().unwrap_or_default();
    let model_path = args.get(1).cloned().unwrap_or_default();
    let mut out = Map::new();
    out.insert("kind".into(), json!(kind));
    out.insert("ok".into(), json!(false));
    let r = (|| -> Result<(), String> {
        let vk = kind == "vulkan";
        let dir = if vk { cfg().vulkan_lib_dir.clone() } else { cfg().cpu_lib_dir.clone() };
        let api = super::llama::api_for(&dir)?;
        out.insert("version".into(), json!(read_version(&dir)));
        let gpu = api.supports_gpu_offload();
        if vk && !gpu {
            return Err("RuntimeError: GPU 오프로드를 지원하지 않는 DLL 이 적재됨".into());
        }
        let t0 = Instant::now();
        let mut m = super::llama::Model::load(api, &model_path, &super::llama::LoadOpts {
            n_ctx: config::GEN_N_CTX, n_batch: config::GEN_N_BATCH, n_threads: config::GEN_THREADS,
            n_gpu_layers: if vk { config::GEN_GPU_LAYERS } else { 0 },
            flash_attn: if vk { config::GEN_FLASH_ATTN_GPU } else { config::GEN_FLASH_ATTN },
        })?;
        out.insert("load_s".into(), pyutil::jf(pyutil::round_to(t0.elapsed().as_secs_f64(), 2)));
        let body = "출장여비 규정에 따라 국내출장 숙박비 상한액은 서울특별시 70,000원, 광역시 60,000원, 그 밖의 지역 50,000원으로 하고 일비는 1일 20,000원을 지급한다. ";
        let prompt = |i: usize| super::prompts::build_prompt(super::prompts::SYSTEM_PROMPT, &super::prompts::build_user_message(
            &format!("측정 질문 {}번 — 서울특별시 숙박비 상한액은 얼마인가요?", i), &vec![body.repeat(2); 3]));
        let n_tok = m.tokenize(&prompt(0), false, true).len();
        let mut times = vec![];
        for i in 0..3 {
            m.reset();
            let t = Instant::now();
            let toks = m.prompt_tokens(&prompt(i));
            m.eval_prompt(&toks)?;
            times.push(t.elapsed().as_secs_f64());
        }
        let sec = (times[1] + times[2]) / 2.0;
        out.insert("ok".into(), json!(true));
        out.insert("gpu_offload".into(), json!(gpu));
        out.insert("prompt_tokens".into(), json!(n_tok));
        out.insert("warm_s".into(), pyutil::jf(pyutil::round_to(times[0], 2)));
        out.insert("prefill_s".into(), pyutil::jf(pyutil::round_to(sec, 3)));
        out.insert("prefill_tps".into(), pyutil::jf(pyutil::round_to(n_tok as f64 / sec, 1)));
        Ok(())
    })();
    if let Err(e) = r {
        out.insert("error".into(), json!(pyutil::head(&e, 200)));
    }
    let ok = out.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    println!("{}", pyutil::json_ascii(&Value::Object(out), false));
    if ok { 0 } else { 3 }
}
