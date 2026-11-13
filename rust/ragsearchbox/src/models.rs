//------------------------------------------------------------------
// 설정 > 모델 — 답변 모델 목록 · 받기 전 추천 · 받기 · 확인 · 설치 · 삭제 (모델 받기 설계서)
//=> 목록 파일(models.json)은 프로그램에 든 것과 배포 사이트(GitHub Pages)의 것 중 새것을 쓴다.
//   모델은 GitHub 릴리스의 모델별 설치 파일(.exe + 조각 .bin)로 받는다.
//    1) 받기: Windows 내장 WinHTTP — 회사 프록시를 그대로 따르고, .part 크기부터 이어 받는다
//    2) 확인: 조각마다 SHA256 을 목록 파일 값과 비교 — 코드 서명 전이라 내려받은 실행 파일의 유일한 확인 수단
//    3) 설치: 설치 파일을 조용히(/VERYSILENT) 실행 → models 에 모델이 생겼는지 확인 → 받은 파일 지우기
//   받기는 한 번에 하나(대기열). 스레드가 상태를 고치고, 설정 창은 1초마다 state_json 으로 읽어 간다.
//------------------------------------------------------------------

use std::collections::{HashMap, VecDeque};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use serde_json::{json, Value};

use crate::hwinfo::Hw;

// 내장 기본 모델 — 기본 설치 파일에 들어 있고 지울 수 없다
pub const BASE_ALIAS: &str = "qwen3-1.7b-q4";
pub const BASE_FILE: &str = "Qwen3-1.7B-Q4_K_M.gguf";
// 프로그램에 든 목록 파일 · 배포 사이트 목록 파일 · 하루 한 번 새로 받는다
const BUILTIN: &str = include_str!("../ui/models.json");
const CATALOG_URL: &str = "https://kobongsoo.github.io/MoAI-S/models.json";
const CATALOG_MAX_AGE: Duration = Duration::from_secs(24 * 3600);
const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
// 받기 실패 때 다시 시도하는 간격(초)
const RETRY_SECS: [u64; 3] = [5, 15, 45];

//------------------------------------------------------------------
// 받기 한 건의 진행
//------------------------------------------------------------------
#[derive(Debug, Clone)]
struct Job {
    alias: String,
    // get | verify | install
    phase: String,
    done: u64,
    total: u64,
    speed: f64,
}

#[derive(Default)]
struct State {
    catalog: Value,
    // 목록 출처 안내(받지 못했을 때만 글이 있다)
    cat_note: String,
    fetching: bool,
    queue: VecDeque<String>,
    cur: Option<Job>,
    cancel: bool,
    running: bool,
    // 모델별 마지막 안내 글(오류 · 결과)
    msgs: HashMap<String, String>,
    // 끝난 받기(트레이 알림용) — (별칭, 성공?, 글)
    finished: Vec<(String, bool, String)>,
}

//------------------------------------------------------------------
// 모델 관리 — 앱이 하나 만들어 들고 있다
//------------------------------------------------------------------
pub struct Models {
    st: Arc<Mutex<State>>,
    // 목록 캐시 파일 · 받은 파일을 둘 폴더
    cache_path: PathBuf,
    dl_root: PathBuf,
}

impl Models {
    //------------------------------------------------------------------
    // 만들기 — 캐시와 내장 목록 중 새것으로 시작한다(인터넷은 모델 탭을 열 때 본다)
    //------------------------------------------------------------------
    pub fn new() -> Models {
        let base = PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into())).join("MoAISearch");
        let cache_path = base.join("models_catalog.json");
        let builtin: Value = serde_json::from_str(BUILTIN).unwrap_or_else(|_| json!({"models": []}));
        let cached = std::fs::read_to_string(&cache_path).ok().and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .filter(valid_catalog);
        let catalog = match cached {
            Some(c) if c["updated"].as_str().unwrap_or("") > builtin["updated"].as_str().unwrap_or("") => c,
            _ => builtin,
        };
        Models {
            st: Arc::new(Mutex::new(State { catalog, ..Default::default() })),
            cache_path,
            dl_root: base.join("downloads"),
        }
    }

    //------------------------------------------------------------------
    // 배포 사이트 목록을 뒤에서 받기 — 캐시가 하루 안이면 그냥 둔다. force 면 늘 받는다
    //------------------------------------------------------------------
    pub fn refresh_catalog(&self, force: bool) {
        let fresh = std::fs::metadata(&self.cache_path).and_then(|m| m.modified()).ok()
            .and_then(|t| SystemTime::now().duration_since(t).ok()).map(|d| d < CATALOG_MAX_AGE).unwrap_or(false);
        {
            let mut g = self.st.lock().unwrap();
            if g.fetching || (fresh && !force && g.cat_note.is_empty()) {
                return;
            }
            g.fetching = true;
        }
        let st = self.st.clone();
        let cache = self.cache_path.clone();
        std::thread::spawn(move || {
            let r = http_text(CATALOG_URL);
            let mut g = st.lock().unwrap();
            g.fetching = false;
            match r.and_then(|s| serde_json::from_str::<Value>(&s).map_err(|e| e.to_string()).map(|v| (s, v))) {
                Ok((raw, v)) if valid_catalog(&v) => {
                    if let Some(d) = cache.parent() {
                        let _ = std::fs::create_dir_all(d);
                    }
                    let _ = std::fs::write(&cache, raw);
                    // 프로그램에 든 목록이 더 새것이면(앱을 막 올린 경우) 그대로 둔다
                    if v["updated"].as_str().unwrap_or("") >= g.catalog["updated"].as_str().unwrap_or("") {
                        g.catalog = v;
                    }
                    g.cat_note.clear();
                }
                Ok(_) => g.cat_note = "모델 목록을 새로 받지 못해(모양이 다름) 프로그램에 들어 있는 목록을 보입니다".into(),
                Err(e) => {
                    crate::info!("models", "목록 받기 실패: {}", e);
                    g.cat_note = "모델 목록을 새로 받지 못해 프로그램에 들어 있는 목록을 보입니다".into();
                }
            }
        });
    }

    // 목록에서 모델 하나
    fn entry(&self, alias: &str) -> Option<Value> {
        let g = self.st.lock().unwrap();
        g.catalog["models"].as_array()?.iter().find(|m| m["alias"] == alias).cloned()
    }

    // 목록 전체(사본)
    pub fn catalog(&self) -> Value {
        self.st.lock().unwrap().catalog.clone()
    }

    //------------------------------------------------------------------
    // 받기 누름 — 대기열에 넣고, 도는 스레드가 없으면 띄운다
    //   models_dir = 모델이 설치될 폴더(디스크 여유 확인용). 돌려줌: Ok(안내) / Err(사유)
    //------------------------------------------------------------------
    pub fn get(&self, alias: &str, models_dir: &str) -> Result<String, String> {
        let m = self.entry(alias).ok_or("목록에 없는 모델입니다")?;
        if m["parts"].as_array().map(|p| p.is_empty()).unwrap_or(true) {
            return Err("이 모델의 설치 파일이 아직 배포되지 않았습니다".into());
        }
        if !app_ok(m["min_app"].as_str().unwrap_or("")) {
            return Err(format!("이 모델은 MoAI-S {} 이상에서 쓸 수 있습니다", m["min_app"].as_str().unwrap_or("")));
        }
        // 설치 중 필요: 받을 파일 + 풀린 모델 + 1GB 여유
        let need = (part_bytes(&m) + m["file_bytes"].as_u64().unwrap_or(0)) / 1_048_576 + 1024;
        let free = crate::hwinfo::disk_free_mb(models_dir);
        if free > 0 && free < need {
            return Err(format!("디스크 여유 {:.1}GB — 이 모델은 설치 중 약 {:.1}GB 가 필요합니다", free as f64 / 1024.0, need as f64 / 1024.0));
        }
        let mut g = self.st.lock().unwrap();
        if g.cur.as_ref().map(|j| j.alias == alias).unwrap_or(false) || g.queue.iter().any(|a| a == alias) {
            return Ok("이미 받는 중입니다".into());
        }
        g.queue.push_back(alias.to_string());
        g.msgs.remove(alias);
        let queued = g.running;
        if !g.running {
            g.running = true;
            g.cancel = false;
            let st = self.st.clone();
            let root = self.dl_root.clone();
            let md = models_dir.to_string();
            std::thread::spawn(move || run_queue(st, root, md));
        }
        Ok(if queued { "대기에 넣었습니다 — 앞 받기가 끝나면 받습니다".into() } else { "받기를 시작합니다".into() })
    }

    //------------------------------------------------------------------
    // 취소 — 받는 중이면 멈추고(받은 조각은 남겨 이어 받기), 대기 중이면 대기에서 뺀다
    //------------------------------------------------------------------
    pub fn cancel(&self, alias: &str) -> String {
        let mut g = self.st.lock().unwrap();
        if let Some(i) = g.queue.iter().position(|a| a == alias) {
            g.queue.remove(i);
            return "대기에서 뺐습니다".into();
        }
        if g.cur.as_ref().map(|j| j.alias == alias && j.phase != "install").unwrap_or(false) {
            g.cancel = true;
            return "멈추는 중입니다 — 받은 부분은 남겨 두어 이어 받을 수 있습니다".into();
        }
        "받는 중이 아닙니다".into()
    }

    //------------------------------------------------------------------
    // 받은 조각 지우기(멈춘 받기)
    //------------------------------------------------------------------
    pub fn discard(&self, alias: &str) -> String {
        if self.busy_with(alias) {
            return "받는 중에는 지울 수 없습니다 — 먼저 취소하세요".into();
        }
        let _ = std::fs::remove_dir_all(self.dl_root.join(safe(alias)));
        "받은 부분을 지웠습니다".into()
    }

    // 그 모델을 받는 중 · 대기 중인가
    pub fn busy_with(&self, alias: &str) -> bool {
        let g = self.st.lock().unwrap();
        g.cur.as_ref().map(|j| j.alias == alias).unwrap_or(false) || g.queue.iter().any(|a| a == alias)
    }

    // 끝난 받기들을 꺼낸다(트레이 알림용)
    pub fn take_finished(&self) -> Vec<(String, bool, String)> {
        std::mem::take(&mut self.st.lock().unwrap().finished)
    }

    // 모델 이름(목록 값, 없으면 별칭)
    pub fn name(&self, alias: &str) -> String {
        self.entry(alias).and_then(|m| m["name"].as_str().map(String::from)).unwrap_or_else(|| alias.to_string())
    }

    // 그 모델의 원본 모델 카드 주소 — 목록 파일 값만 쓴다(설정 창이 보낸 주소는 쓰지 않는다)
    pub fn source_url(&self, alias: &str) -> Option<String> {
        self.entry(alias)?["source_url"].as_str().filter(|u| u.starts_with("https://")).map(String::from)
    }

    // 배포 페이지 주소
    pub fn release_url(&self) -> String {
        self.st.lock().unwrap().catalog["release"].as_str().filter(|u| u.starts_with("https://"))
            .unwrap_or("https://github.com/kobongsoo/MoAI-S/releases").to_string()
    }

    //------------------------------------------------------------------
    // 라이선스 글 — 설치했으면 licenses\models\<별칭>.txt, 아니면 목록의 요약
    //------------------------------------------------------------------
    pub fn license_text(&self, alias: &str, dirs: &[PathBuf]) -> String {
        for d in dirs {
            let p = d.parent().map(|a| a.join("licenses").join("models").join(format!("{}.txt", safe(alias))));
            if let Some(t) = p.and_then(|p| std::fs::read_to_string(p).ok()) {
                return t;
            }
        }
        match self.entry(alias) {
            Some(m) => format!("{}\n\n{}\n\n전문: {}", m["license"].as_str().unwrap_or(""), m["license_note"].as_str().unwrap_or(""),
                               m["source_url"].as_str().unwrap_or("")),
            None => String::new(),
        }
    }

    //------------------------------------------------------------------
    // 설치 모델 지우기 — 내장 · 사용 중 모델은 막는다. 모델 · 설명 파일 · 라이선스 글을 지운다
    //------------------------------------------------------------------
    pub fn delete(&self, alias: &str, current: &str, dirs: &[PathBuf]) -> Result<String, String> {
        let m = self.entry(alias).ok_or("목록에 없는 모델입니다")?;
        if m["builtin"].as_bool().unwrap_or(false) {
            return Err("내장 모델은 지울 수 없습니다".into());
        }
        if alias == current {
            return Err("지금 쓰는 모델은 지울 수 없습니다 — 다른 모델을 먼저 사용하세요".into());
        }
        let file = m["file"].as_str().unwrap_or("");
        let mut n = 0;
        for d in dirs {
            let p = d.join(file);
            if !file.is_empty() && p.is_file() {
                std::fs::remove_file(&p).map_err(|e| format!("지우지 못했습니다: {}", e))?;
                n += 1;
            }
            let _ = std::fs::remove_file(d.join(format!("{}.model.json", safe(alias))));
            if let Some(app) = d.parent() {
                let _ = std::fs::remove_file(app.join("licenses").join("models").join(format!("{}.txt", safe(alias))));
            }
        }
        crate::info!("models", "모델 지움: {} ({}개)", alias, n);
        if n == 0 { Err("설치된 파일이 없습니다".into()) } else { Ok(format!("{} 를 지웠습니다", m["name"].as_str().unwrap_or(alias))) }
    }

    //------------------------------------------------------------------
    // 설정 창에 보낼 값 — 사양 · 추천 · 줄들 · (with_info 면) 상세 화면 내용
    //   current = 지금 쓰는 별칭 · dirs = 모델을 찾을 폴더들 · locked = 답변 중이라 "사용"을 잠글지
    //------------------------------------------------------------------
    pub fn state_json(&self, hw: &Hw, current: &str, dirs: &[PathBuf], locked: bool, with_info: bool) -> Value {
        let g = self.st.lock().unwrap();
        let cat = &g.catalog;
        let rec = &cat["recommend"];
        let all: Vec<&Value> = cat["models"].as_array().map(|a| a.iter().collect()).unwrap_or_default();
        let base = base_ttft(hw, rec, &all);
        // 줄마다 판정
        let mut rows = Vec::new();
        let mut info = serde_json::Map::new();
        for m in &all {
            let alias = m["alias"].as_str().unwrap_or("");
            let status = m["status"].as_str().unwrap_or("ready");
            let installed = installed(m, dirs);
            // 숨김(측정 전)은 아예 안 보이고, 내놓지 않는 모델(retired)은 설치한 사람에게만 보인다
            if status == "hidden" || (status == "retired" && !installed) {
                continue;
            }
            let est = estimate(m, hw, rec, base);
            let mut state = if m["builtin"].as_bool().unwrap_or(false) { "builtin" } else if installed { "installed" } else { "none" };
            let mut row = json!({"alias": alias, "name": m["name"], "vendor": m["vendor"], "tier": m["tier"],
                                 "summary": m["summary"], "korean": m["korean"], "size_mb": m["file_bytes"].as_u64().unwrap_or(0) / 1_048_576,
                                 "rank": m["rank"], "retired": status == "retired", "icon": m["icon"],
                                 "est": est.ttft, "grade": est.grade, "by_spec": est.by_spec, "mem_ok": est.mem_ok,
                                 "need_gb": (est.need_mb as f64 / 102.4).round() / 10.0,
                                 "available": m["parts"].as_array().map(|p| !p.is_empty()).unwrap_or(false),
                                 "app_ok": app_ok(m["min_app"].as_str().unwrap_or("")), "min_app": m["min_app"],
                                 "msg": g.msgs.get(alias).cloned().unwrap_or_default()});
            if let Some(j) = g.cur.as_ref().filter(|j| j.alias == alias) {
                state = match j.phase.as_str() { "verify" => "verifying", "install" => "installing", _ => "downloading" };
                let left = j.total.saturating_sub(j.done) as f64;
                row["pct"] = json!(if j.total > 0 { (j.done * 100 / j.total) as i64 } else { 0 });
                row["done_mb"] = json!(j.done / 1_048_576);
                row["total_mb"] = json!(j.total / 1_048_576);
                row["speed"] = json!((j.speed / 1_048_576.0 * 10.0).round() / 10.0);
                row["eta"] = json!(if j.speed > 1.0 { (left / j.speed).round() as i64 } else { -1 });
            } else if g.queue.iter().any(|a| a == alias) {
                state = "queued";
            } else if state == "none" && self.dl_root.join(safe(alias)).is_dir() {
                state = "paused";
            }
            if alias == current {
                state = "inuse";
            }
            row["state"] = json!(state);
            row["locked"] = json!(locked);
            rows.push(row);
            if with_info {
                info.insert(alias.to_string(), json!({"intro": m["intro"], "released": m["released"], "languages": m["languages"],
                    "params_b": m["params_b"], "bench": m["bench"], "pros": m["pros"], "cons": m["cons"],
                    "license": m["license"], "license_note": m["license_note"], "has_source": m["source_url"].is_string(),
                    "ttft_ratio": m["ttft_ratio"]}));
            }
        }
        // 추천 — 묶음마다 하나. 받을 수 없는 모델(설치 파일이 아직 없거나 앱 판이 낮음)은 이미 설치한 게 아니면 뺀다
        let pick = |tier: &str, max: f64| -> Option<String> {
            rows.iter().filter(|r| r["tier"] == tier && !r["retired"].as_bool().unwrap_or(false) && r["mem_ok"].as_bool().unwrap_or(false))
                .filter(|r| r["state"] != "none" || (r["available"].as_bool().unwrap_or(false) && r["app_ok"].as_bool().unwrap_or(false)))
                .filter(|r| r["rank"].as_i64().unwrap_or(0) > 0 && r["est"].as_f64().map(|t| t <= max).unwrap_or(false))
                .min_by_key(|r| r["rank"].as_i64().unwrap_or(99)).and_then(|r| r["alias"].as_str().map(String::from))
        };
        let rec_base = pick("base", rec["base_ttft_max"].as_f64().unwrap_or(4.0));
        let rec_prec = pick("precise", rec["precise_ttft_max"].as_f64().unwrap_or(10.0));
        for r in rows.iter_mut() {
            let a = r["alias"].as_str().map(String::from);
            r["recommended"] = json!(a.is_some() && (a == rec_base || a == rec_prec));
        }
        // 정밀 묶음에 추천이 없을 때 보일 가장 빠른 정밀 모델 시간
        //   한도(precise_ttft_max)를 넘을 때만 — 받을 수 없어서 추천이 없는 것과 섞지 않는다
        let prec_max = rec["precise_ttft_max"].as_f64().unwrap_or(10.0);
        let prec_fastest = rows.iter().filter(|r| r["tier"] == "precise" && !r["retired"].as_bool().unwrap_or(false))
            .filter_map(|r| r["est"].as_f64()).fold(None, |a: Option<f64>, t| Some(a.map_or(t, |x| x.min(t))))
            .filter(|t| *t > prec_max);
        let cur_ttft = rows.iter().find(|r| r["alias"] == current).and_then(|r| r["est"].as_f64());
        let mut out = json!({
            "hw": hw.to_json(), "base_by_spec": base.1, "current": current, "current_ttft": cur_ttft,
            "current_name": all.iter().find(|m| m["alias"] == current).and_then(|m| m["name"].as_str()).unwrap_or(current),
            "rec_base": rec_base, "rec_prec": rec_prec, "prec_fastest": prec_fastest,
            "rows": rows, "note": g.cat_note, "fetching": g.fetching,
        });
        if with_info {
            out["info"] = Value::Object(info);
        }
        out
    }
}

// 목록 파일 모양이 맞나
fn valid_catalog(v: &Value) -> bool {
    v["schema"].as_i64() == Some(1) && v["models"].is_array()
}

// 받을 파일 크기 합
fn part_bytes(m: &Value) -> u64 {
    m["parts"].as_array().map(|p| p.iter().map(|x| x["bytes"].as_u64().unwrap_or(0)).sum()).unwrap_or(0)
}

// 파일 · 폴더 이름에 쓸 별칭(영숫자 · . - _ 만)
fn safe(alias: &str) -> String {
    alias.chars().filter(|c| c.is_ascii_alphanumeric() || ".-_".contains(*c)).collect()
}

//------------------------------------------------------------------
// 이 앱 판이 그 모델을 쓸 수 있나 — min_app 이 비었거나 지금 판 이하면 참
//------------------------------------------------------------------
pub fn app_ok(min_app: &str) -> bool {
    let p = |s: &str| -> Vec<u64> { s.trim().split('.').map(|x| x.parse().unwrap_or(0)).collect() };
    min_app.trim().is_empty() || p(APP_VERSION) >= p(min_app)
}

//------------------------------------------------------------------
// 설치되어 있나 — 모델을 찾을 폴더 어디든 그 파일이 있으면
//------------------------------------------------------------------
pub fn installed(m: &Value, dirs: &[PathBuf]) -> bool {
    let file = m["file"].as_str().unwrap_or("");
    !file.is_empty() && dirs.iter().any(|d| d.join(file).is_file())
}

//------------------------------------------------------------------
// 추천 계산 (모델 받기 설계서 §4-2)
//------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq)]
pub struct Est {
    pub ttft: Option<f64>,
    pub grade: &'static str,
    pub by_spec: bool,
    pub mem_ok: bool,
    pub need_mb: u64,
}

//------------------------------------------------------------------
// 내장 1.7B 의 이 PC 예상 첫 글자(초) — (값, 사양으로 어림했나)
//=> 잰 속도(gen_backend.json)가 있으면 prompt_tokens ÷ 그 속도. 없으면 사양 등급으로 속도를 어림한다:
//   외장 GPU 4GB 이상 600 · 그래픽 장치 있음(iGPU) 200 · CPU 8스레드 이상 80 · 그 밖 40 토큰/초
//------------------------------------------------------------------
pub fn base_ttft(hw: &Hw, rec: &Value, models: &[&Value]) -> (f64, bool) {
    let tokens = rec["prompt_tokens"].as_f64().unwrap_or(600.0);
    if hw.prefill_tps > 0.0 {
        return (tokens / hw.prefill_tps, false);
    }
    // 1.7B 는 안 쟀지만 다른 모델(예: 옮겨 온 Qwen3-4B)을 쟀으면 그 모델의 첫 글자 비로 나눠 환산한다
    for (file, _, tps) in &hw.probes {
        let ratio = models.iter().find(|m| m["file"] == file.as_str()).and_then(|m| m["ttft_ratio"].as_f64());
        if let Some(r) = ratio.filter(|r| *r > 0.0) {
            return (tokens / tps / r, false);
        }
    }
    let tps = if hw.dgpu_mb() >= 4096 {
        600.0
    } else if hw.backend == "cpu" {
        if hw.threads >= 8 { 80.0 } else { 40.0 }
    } else if !hw.gpus.is_empty() {
        200.0
    } else if hw.threads >= 8 {
        80.0
    } else {
        40.0
    };
    (tokens / tps, true)
}

//------------------------------------------------------------------
// 모델 하나의 판정 — 예상 첫 글자 · 빠름/보통/느림 · 메모리로 돌릴 수 있나
//   필요 메모리 = 모델 파일 + overhead_gb(대화 기억 공간 + 앱 · 임베딩 · 리랭커 · 화면)
//------------------------------------------------------------------
pub fn estimate(m: &Value, hw: &Hw, rec: &Value, base: (f64, bool)) -> Est {
    let need_mb = m["file_bytes"].as_u64().unwrap_or(0) / 1_048_576 + (rec["overhead_gb"].as_f64().unwrap_or(2.1) * 1024.0) as u64;
    let mem_ok = hw.ram_total_mb == 0 || hw.ram_total_mb >= need_mb;
    let ttft = m["ttft_ratio"].as_f64().map(|r| ((base.0 * r) * 10.0).round() / 10.0);
    let grade = match ttft {
        None => "",
        Some(t) if t <= rec["base_ttft_max"].as_f64().unwrap_or(4.0) => "빠름",
        Some(t) if t <= rec["precise_ttft_max"].as_f64().unwrap_or(10.0) => "보통",
        Some(_) => "느림",
    };
    Est { ttft, grade, by_spec: base.1, mem_ok, need_mb }
}

//------------------------------------------------------------------
// 대기열 돌리기(스레드) — 하나씩 받기 → 확인 → 설치. 대기열이 비면 끝난다
//------------------------------------------------------------------
fn run_queue(st: Arc<Mutex<State>>, root: PathBuf, models_dir: String) {
    loop {
        let (alias, m, base_url) = {
            let mut g = st.lock().unwrap();
            let Some(a) = g.queue.pop_front() else {
                g.running = false;
                g.cur = None;
                return;
            };
            let m = g.catalog["models"].as_array().and_then(|v| v.iter().find(|x| x["alias"] == a.as_str()).cloned()).unwrap_or(Value::Null);
            let base = g.catalog["download_base"].as_str().unwrap_or("").to_string();
            g.cancel = false;
            g.cur = Some(Job { alias: a.clone(), phase: "get".into(), done: 0, total: part_bytes(&m), speed: 0.0 });
            (a, m, base)
        };
        crate::info!("models", "받기 시작: {}", alias);
        let r = run_job(&st, &root, &models_dir, &alias, &m, &base_url);
        let mut g = st.lock().unwrap();
        g.cur = None;
        let name = m["name"].as_str().unwrap_or(&alias).to_string();
        match r {
            Ok(()) => {
                crate::info!("models", "설치 끝: {}", alias);
                g.msgs.insert(alias.clone(), "설치했습니다 — [사용] 을 누르면 이 모델로 답합니다".into());
                g.finished.push((alias, true, format!("{} 를 설치했습니다 — 설정 > 모델에서 [사용] 을 누르세요", name)));
            }
            Err(e) if e == CANCELLED => {
                crate::info!("models", "받기 멈춤: {}", alias);
                g.msgs.insert(alias, "멈췄습니다 — [이어 받기] 로 받은 데서부터 다시 받습니다".into());
            }
            Err(e) => {
                crate::warn!("models", "받기 실패: {} — {}", alias, e);
                g.msgs.insert(alias.clone(), e.clone());
                g.finished.push((alias, false, format!("{} 를 설치하지 못했습니다 — {}", name, e)));
            }
        }
    }
}

const CANCELLED: &str = "취소됨";

// 진행 고치기
fn set_job(st: &Arc<Mutex<State>>, f: impl FnOnce(&mut Job)) {
    if let Some(j) = st.lock().unwrap().cur.as_mut() {
        f(j);
    }
}
fn cancelled(st: &Arc<Mutex<State>>) -> bool {
    st.lock().unwrap().cancel
}

//------------------------------------------------------------------
// 모델 하나 받기 → 확인 → 설치. 돌려줌: Ok / Err(사람이 읽을 사유 · 취소면 CANCELLED)
//------------------------------------------------------------------
fn run_job(st: &Arc<Mutex<State>>, root: &Path, models_dir: &str, alias: &str, m: &Value, base_url: &str) -> Result<(), String> {
    let parts = m["parts"].as_array().cloned().unwrap_or_default();
    if parts.is_empty() || base_url.is_empty() {
        return Err("이 모델의 설치 파일이 아직 배포되지 않았습니다".into());
    }
    let dir = root.join(safe(alias));
    std::fs::create_dir_all(&dir).map_err(|e| format!("받을 폴더를 만들지 못했습니다: {}", e))?;
    // 1) 받기 — 다 받은 조각은 건너뛰고, .part 는 이어 받는다
    let mut before = 0u64;
    for p in &parts {
        let name = p["name"].as_str().unwrap_or("");
        let bytes = p["bytes"].as_u64().unwrap_or(0);
        if name.is_empty() || name.contains(['/', '\\']) {
            return Err("목록 파일의 설치 파일 이름이 잘못되었습니다".into());
        }
        let done_path = dir.join(name);
        if done_path.is_file() && file_len(&done_path) == bytes {
            before += bytes;
            set_job(st, |j| j.done = before);
            continue;
        }
        let part_path = dir.join(format!("{}.part", name));
        let url = format!("{}{}", base_url, name);
        let mut tries = 0;
        loop {
            match download(st, &url, &part_path, before, bytes) {
                Ok(()) => break,
                Err(e) if e == CANCELLED => return Err(e),
                // 404 등은 다시 해도 같다 — 아직 릴리스에 올리지 않았거나 목록이 옛것
                Err(e) if e.starts_with("HTTP 4") => {
                    return Err(format!("배포 파일을 찾지 못했습니다({}) — 아직 올라가지 않았을 수 있습니다. 잠시 뒤 다시 받거나 배포 페이지를 확인하세요", e));
                }
                Err(e) => {
                    if tries >= RETRY_SECS.len() {
                        return Err(format!("배포 서버에 연결하지 못했습니다(회사망에서 막혔을 수 있습니다) — {}", e));
                    }
                    crate::info!("models", "받기 끊김({}), {}초 뒤 다시: {}", name, RETRY_SECS[tries], e);
                    for _ in 0..RETRY_SECS[tries] * 10 {
                        if cancelled(st) {
                            return Err(CANCELLED.into());
                        }
                        std::thread::sleep(Duration::from_millis(100));
                    }
                    tries += 1;
                }
            }
        }
        if bytes > 0 && file_len(&part_path) != bytes {
            return Err(format!("받은 크기가 다릅니다({} ≠ {})", file_len(&part_path), bytes));
        }
        std::fs::rename(&part_path, &done_path).map_err(|e| format!("받은 파일 이름을 바꾸지 못했습니다: {}", e))?;
        before += bytes;
    }
    // 2) 확인 — 다르면 그 조각을 지운다(다시 받기)
    set_job(st, |j| { j.phase = "verify".into(); j.speed = 0.0; });
    for p in &parts {
        let path = dir.join(p["name"].as_str().unwrap_or(""));
        let want = p["sha256"].as_str().unwrap_or("").to_lowercase();
        let got = sha256_file(&path).map_err(|e| format!("받은 파일을 읽지 못했습니다: {}", e))?;
        if want.len() != 64 || got != want {
            let _ = std::fs::remove_file(&path);
            return Err("받은 파일이 손상되었습니다 — [받기] 로 다시 받으세요".into());
        }
        if cancelled(st) {
            return Err(CANCELLED.into());
        }
    }
    // 3) 설치 — 사용자 권한으로 조용히. 모델 파일이 생겼는지로 끝을 판단한다
    set_job(st, |j| j.phase = "install".into());
    let exe = dir.join(parts[0]["name"].as_str().unwrap_or(""));
    let code = std::process::Command::new(&exe)
        .args(["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"])
        .current_dir(&dir)
        .status()
        .map_err(|e| format!("설치 파일을 실행하지 못했습니다: {}", e))?
        .code()
        .unwrap_or(-1);
    if code != 0 {
        return Err(format!("설치하지 못했습니다(코드 {}) — MoAI Search 가 설치된 PC 인지 확인하세요", code));
    }
    let file = m["file"].as_str().unwrap_or("");
    let target = Path::new(models_dir).join(file);
    if !target.is_file() {
        return Err(format!("설치는 끝났지만 {} 가 models 폴더에 없습니다", file));
    }
    // 받은 설치 파일은 지운다(정밀 모델은 2.7GB)
    let _ = std::fs::remove_dir_all(&dir);
    Ok(())
}

fn file_len(p: &Path) -> u64 {
    std::fs::metadata(p).map(|m| m.len()).unwrap_or(0)
}

//------------------------------------------------------------------
// 파일 SHA256 (소문자 16진)
//------------------------------------------------------------------
pub fn sha256_file(p: &Path) -> std::io::Result<String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut f = std::fs::File::open(p)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(h.finalize().iter().map(|b| format!("{:02x}", b)).collect())
}

//------------------------------------------------------------------
// 조각 하나 받기 — .part 크기부터 Range 로 이어 받는다
//   before = 앞 조각들 합(진행 표시용) · expect = 이 조각 크기(0 이면 모름)
//------------------------------------------------------------------
fn download(st: &Arc<Mutex<State>>, url: &str, part: &Path, before: u64, expect: u64) -> Result<(), String> {
    let mut have = file_len(part);
    if expect > 0 && have == expect {
        return Ok(());
    }
    if expect > 0 && have > expect {
        let _ = std::fs::remove_file(part);
        have = 0;
    }
    let mut t0 = Instant::now();
    let mut n0 = have;
    let mut file: Option<std::fs::File> = None;
    let r = http::get(url, have, &mut |status, chunk| {
        // 200 이면 서버가 이어 받기를 안 받아 줬다 — 처음부터 다시 쓴다
        if file.is_none() {
            let append = status == 206;
            if !append {
                have = 0;
                n0 = 0;
            }
            file = Some(std::fs::OpenOptions::new().create(true).write(true).append(append).truncate(!append).open(part)
                .map_err(|e| e.to_string())?);
        }
        file.as_mut().unwrap().write_all(chunk).map_err(|e| e.to_string())?;
        have += chunk.len() as u64;
        let el = t0.elapsed().as_secs_f64();
        let mut g = st.lock().unwrap();
        if g.cancel {
            return Err(CANCELLED.into());
        }
        if let Some(j) = g.cur.as_mut() {
            j.done = before + have;
            // 1초마다 속도를 새로 잰다(앞 값과 반씩 섞어 흔들림을 줄인다)
            if el >= 1.0 {
                let now = (have - n0) as f64 / el;
                j.speed = if j.speed > 0.0 { (j.speed + now) / 2.0 } else { now };
                t0 = Instant::now();
                n0 = have;
            }
        }
        Ok(())
    });
    if let Some(f) = file.as_mut() {
        let _ = f.flush();
    }
    match r {
        // 416 = 이미 다 받았다
        Err(e) if e.starts_with("HTTP 416") && expect > 0 && file_len(part) == expect => Ok(()),
        other => other,
    }
}

//------------------------------------------------------------------
// 작은 글 받기(목록 파일)
//------------------------------------------------------------------
fn http_text(url: &str) -> Result<String, String> {
    let mut buf = Vec::new();
    http::get(url, 0, &mut |status, chunk| {
        if status != 200 {
            return Err(format!("HTTP {}", status));
        }
        buf.extend_from_slice(chunk);
        if buf.len() > 4 << 20 {
            return Err("너무 큼".into());
        }
        Ok(())
    })?;
    String::from_utf8(buf).map_err(|e| e.to_string())
}

//------------------------------------------------------------------
// WinHTTP — Windows 내장 HTTP. 프록시는 시스템 설정(자동 구성 포함), 다른 주소로 넘김은 자동으로 따라간다
//------------------------------------------------------------------
mod http {
    use std::ffi::c_void;
    use windows::core::{HSTRING, PCWSTR};
    use windows::Win32::Networking::WinHttp::*;

    // 핸들을 쓰고 나면 닫는다
    struct H(*mut c_void);
    impl Drop for H {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe {
                    let _ = WinHttpCloseHandle(self.0);
                }
            }
        }
    }

    // "https://host[:port]/path" → (https?, host, port, path)
    pub fn split_url(url: &str) -> Option<(bool, String, u16, String)> {
        let (secure, rest) = if let Some(r) = url.strip_prefix("https://") {
            (true, r)
        } else {
            (false, url.strip_prefix("http://")?)
        };
        let (hostport, path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let (host, port) = match hostport.rsplit_once(':') {
            Some((h, p)) => (h, p.parse().ok()?),
            None => (hostport, if secure { 443 } else { 80 }),
        };
        if host.is_empty() {
            return None;
        }
        Some((secure, host.to_string(), port, path.to_string()))
    }

    //------------------------------------------------------------------
    // GET — offset > 0 이면 Range 를 붙인다. 받은 덩어리마다 on_chunk(상태 코드, 덩어리)를 부른다
    //   상태 코드가 200 · 206 이 아니면 Err("HTTP <코드>")
    //------------------------------------------------------------------
    pub fn get(url: &str, offset: u64, on_chunk: &mut dyn FnMut(u32, &[u8]) -> Result<(), String>) -> Result<(), String> {
        let (secure, host, port, path) = split_url(url).ok_or("주소가 잘못되었습니다")?;
        unsafe {
            let agent = HSTRING::from(format!("MoAISearch/{}", super::APP_VERSION));
            let s = H(WinHttpOpen(&agent, WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, PCWSTR::null(), PCWSTR::null(), 0));
            if s.0.is_null() {
                return Err(format!("연결 준비 실패: {}", windows::core::Error::from_win32()));
            }
            // 이름 찾기 · 연결 15초, 보내기 30초, 받기 60초
            let _ = WinHttpSetTimeouts(s.0, 15_000, 15_000, 30_000, 60_000);
            let c = H(WinHttpConnect(s.0, &HSTRING::from(host.as_str()), port, 0));
            if c.0.is_null() {
                return Err(format!("연결 실패: {}", windows::core::Error::from_win32()));
            }
            let flags = if secure { WINHTTP_FLAG_SECURE } else { WINHTTP_OPEN_REQUEST_FLAGS(0) };
            let r = H(WinHttpOpenRequest(c.0, &HSTRING::from("GET"), &HSTRING::from(path.as_str()), PCWSTR::null(), PCWSTR::null(),
                                         std::ptr::null(), flags));
            if r.0.is_null() {
                return Err(format!("요청 실패: {}", windows::core::Error::from_win32()));
            }
            if offset > 0 {
                let h: Vec<u16> = format!("Range: bytes={}-", offset).encode_utf16().collect();
                WinHttpAddRequestHeaders(r.0, &h, WINHTTP_ADDREQ_FLAG_ADD).map_err(|e| e.to_string())?;
            }
            WinHttpSendRequest(r.0, None, None, 0, 0, 0).map_err(|e| e.message())?;
            WinHttpReceiveResponse(r.0, std::ptr::null_mut()).map_err(|e| e.message())?;
            let mut status = 0u32;
            let mut len = 4u32;
            WinHttpQueryHeaders(r.0, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, PCWSTR::null(),
                                Some(&mut status as *mut u32 as *mut c_void), &mut len, std::ptr::null_mut()).map_err(|e| e.message())?;
            if status != 200 && status != 206 {
                return Err(format!("HTTP {}", status));
            }
            let mut buf = vec![0u8; 256 * 1024];
            loop {
                let mut n = 0u32;
                WinHttpReadData(r.0, buf.as_mut_ptr() as *mut c_void, buf.len() as u32, &mut n).map_err(|e| e.message())?;
                if n == 0 {
                    break;
                }
                on_chunk(status, &buf[..n as usize])?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hwinfo::Gpu;

    fn rec() -> Value {
        json!({"base_ttft_max": 4.0, "precise_ttft_max": 10.0, "overhead_gb": 2.1, "prompt_tokens": 600})
    }
    fn hw(ram_gb: u64, tps: f64, backend: &str, gpus: Vec<Gpu>, threads: usize) -> Hw {
        Hw { ram_total_mb: ram_gb * 1024, prefill_tps: tps, backend: backend.into(), gpus, threads, ..Default::default() }
    }
    fn model(file_mb: u64, ratio: f64) -> Value {
        json!({"file_bytes": file_mb * 1_048_576, "ttft_ratio": ratio})
    }

    // 설계서 §4-3 의 예 — 16GB · iGPU 260 t/s 면 1.7B 2.3초 · Qwen3.5-4B 7.6초(보통)
    #[test]
    fn estimate_dev_pc() {
        let h = hw(16, 260.0, "vulkan", vec![Gpu { name: "Intel".into(), vram_mb: 128 }], 16);
        let b = base_ttft(&h, &rec(), &[]);
        assert!(!b.1);
        let e = estimate(&model(2614, 3.3), &h, &rec(), b);
        assert_eq!(e.ttft, Some(7.6));
        assert_eq!(e.grade, "보통");
        assert!(e.mem_ok);
        assert_eq!(estimate(&model(1056, 1.0), &h, &rec(), b).grade, "빠름");
    }

    // 8GB · CPU 만(80 t/s) — 4B 는 느림, 메모리는 겨우 들어감
    #[test]
    fn estimate_low_pc() {
        let h = hw(8, 80.0, "cpu", vec![], 8);
        let b = base_ttft(&h, &rec(), &[]);
        let e = estimate(&model(2614, 3.3), &h, &rec(), b);
        assert_eq!(e.grade, "느림");
        assert!(e.mem_ok);
        // 4GB 면 4B 는 메모리 부족
        assert!(!estimate(&model(2614, 3.3), &hw(4, 80.0, "cpu", vec![], 4), &rec(), b).mem_ok);
    }

    // 잰 속도가 없으면 사양 등급으로 — 외장 8GB > iGPU > CPU 스레드
    #[test]
    fn base_by_spec() {
        let d = hw(32, 0.0, "", vec![Gpu { name: "RTX".into(), vram_mb: 8192 }], 16);
        assert_eq!(base_ttft(&d, &rec(), &[]), (1.0, true));
        let i = hw(16, 0.0, "", vec![Gpu { name: "Iris".into(), vram_mb: 128 }], 8);
        assert_eq!(base_ttft(&i, &rec(), &[]), (3.0, true));
        let c = hw(8, 0.0, "", vec![], 4);
        assert_eq!(base_ttft(&c, &rec(), &[]), (15.0, true));
    }

    // 1.7B 는 안 쟀고 Qwen3-4B 만 쟀으면 그 모델의 첫 글자 비로 나눠 환산한다(사양 어림이 아님)
    #[test]
    fn base_from_other_probe() {
        let mut h = hw(16, 0.0, "vulkan", vec![], 16);
        h.probes = vec![("Qwen3-4B-Q4_K_M.gguf".into(), "vulkan".into(), 100.0)];
        let m = json!({"file": "Qwen3-4B-Q4_K_M.gguf", "ttft_ratio": 3.0});
        let (t, by_spec) = base_ttft(&h, &rec(), &[&m]);
        assert!(!by_spec);
        assert!((t - 2.0).abs() < 1e-9);
    }

    #[test]
    fn version_gate() {
        assert!(app_ok(""));
        assert!(app_ok("0.0.1"));
        assert!(!app_ok("99.0.0"));
    }

    #[test]
    fn url_split() {
        assert_eq!(http::split_url("https://github.com/a/b.exe"), Some((true, "github.com".into(), 443, "/a/b.exe".into())));
        assert_eq!(http::split_url("http://127.0.0.1:8765/x"), Some((false, "127.0.0.1".into(), 8765, "/x".into())));
        assert_eq!(http::split_url("ftp://x"), None);
    }

    //------------------------------------------------------------------
    // 시험용 HTTP 서버 — Range 를 받으면 206 으로 나머지를, 첫 연결은 절반만 보내고 끊는다(끊김 흉내)
    //------------------------------------------------------------------
    fn serve(data: Vec<u8>, conns: usize) -> String {
        use std::io::{BufRead, BufReader};
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap();
        std::thread::spawn(move || {
            for (i, s) in l.incoming().take(conns).enumerate() {
                let mut s = s.unwrap();
                let mut r = BufReader::new(s.try_clone().unwrap());
                let mut from = 0usize;
                loop {
                    let mut line = String::new();
                    if r.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some(v) = line.to_lowercase().strip_prefix("range: bytes=") {
                        from = v.trim().trim_end_matches('-').parse().unwrap_or(0);
                    }
                }
                let body = &data[from..];
                let head = if from > 0 {
                    format!("HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {}-{}/{}\r\nConnection: close\r\n\r\n",
                            body.len(), from, data.len() - 1, data.len())
                } else {
                    format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len())
                };
                let _ = s.write_all(head.as_bytes());
                // 첫 연결은 절반만 보내고 끊는다
                let n = if i == 0 { body.len() / 2 } else { body.len() };
                let _ = s.write_all(&body[..n]);
                let _ = s.flush();
            }
        });
        format!("http://{}/m.bin", addr)
    }

    #[test]
    fn download_resumes() {
        let data: Vec<u8> = (0..3_000_000u32).map(|i| (i * 7 % 251) as u8).collect();
        let url = serve(data.clone(), 2);
        let part = std::env::temp_dir().join(format!("rsb_dl_{}.part", std::process::id()));
        let _ = std::fs::remove_file(&part);
        let st = Arc::new(Mutex::new(State { cur: Some(Job { alias: "t".into(), phase: "get".into(), done: 0, total: 3_000_000, speed: 0.0 }), ..Default::default() }));
        // 첫 번째는 끊겨 일부만 — 오류이거나 덜 받은 채 끝난다
        let _ = download(&st, &url, &part, 0, data.len() as u64);
        let half = file_len(&part);
        assert!(half > 0 && half < data.len() as u64, "half={}", half);
        // 두 번째는 Range 로 나머지
        download(&st, &url, &part, 0, data.len() as u64).unwrap();
        assert_eq!(std::fs::read(&part).unwrap(), data);
        assert_eq!(st.lock().unwrap().cur.as_ref().unwrap().done, data.len() as u64);
        // SHA256 이 맞게 나온다(빈 파일의 잘 알려진 값으로 확인)
        let empty = std::env::temp_dir().join(format!("rsb_empty_{}", std::process::id()));
        std::fs::write(&empty, b"").unwrap();
        assert_eq!(sha256_file(&empty).unwrap(), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
        let _ = std::fs::remove_file(&part);
        let _ = std::fs::remove_file(&empty);
    }

    //------------------------------------------------------------------
    // 받기 → 확인 → 설치 전 과정(손으로만 돌린다) — 만든 설치 파일을 로컬 HTTP 로 내놓고 run_queue 를 돌린다
    //   MODEL_E2E_BASE = http://127.0.0.1:<포트>/ · MODEL_E2E_CATALOG = 빌드가 쓴 models.json
    //   MODEL_E2E_ALIAS = 받을 별칭 · MODEL_E2E_MODELS = 설치 폴더의 models (임시 레지스트리 설치 위치 아래)
    //------------------------------------------------------------------
    #[test]
    #[ignore]
    fn e2e_install() {
        let env = |k: &str| std::env::var(k).unwrap();
        let mut cat: Value = serde_json::from_str(&std::fs::read_to_string(env("MODEL_E2E_CATALOG")).unwrap()).unwrap();
        cat["download_base"] = json!(env("MODEL_E2E_BASE"));
        let alias = env("MODEL_E2E_ALIAS");
        let root = std::env::temp_dir().join(format!("rsb_e2e_dl_{}", std::process::id()));
        let st = Arc::new(Mutex::new(State { catalog: cat, running: true, ..Default::default() }));
        st.lock().unwrap().queue.push_back(alias.clone());
        run_queue(st.clone(), root.clone(), env("MODEL_E2E_MODELS"));
        let g = st.lock().unwrap();
        println!("msgs={:?} finished={:?}", g.msgs, g.finished);
        assert!(g.finished.iter().any(|f| f.0 == alias && f.1), "설치 실패");
        // 받은 설치 파일은 지워졌다
        assert!(!root.join(safe(&alias)).exists());
    }

    // 실제 배포 주소에서 받기(손으로만) — GitHub 릴리스는 다른 주소로 넘겨 준다. 반을 받은 척하고 이어 받는다
    //   MODEL_URL · MODEL_SIZE · MODEL_SHA
    #[test]
    #[ignore]
    fn real_url_download() {
        let env = |k: &str| std::env::var(k).unwrap();
        let size: u64 = env("MODEL_SIZE").parse().unwrap();
        let part = std::env::temp_dir().join(format!("rsb_real_{}.part", std::process::id()));
        let st = Arc::new(Mutex::new(State { cur: Some(Job { alias: "t".into(), phase: "get".into(), done: 0, total: size, speed: 0.0 }), ..Default::default() }));
        download(&st, &env("MODEL_URL"), &part, 0, size).unwrap();
        // 뒤 절반을 잘라 내고 이어 받기
        let f = std::fs::OpenOptions::new().write(true).open(&part).unwrap();
        f.set_len(size / 2).unwrap();
        drop(f);
        download(&st, &env("MODEL_URL"), &part, 0, size).unwrap();
        assert_eq!(file_len(&part), size);
        assert_eq!(sha256_file(&part).unwrap(), env("MODEL_SHA"));
        let _ = std::fs::remove_file(&part);
    }

    // 프로그램에 든 목록 파일이 모양에 맞고, 내장 모델이 들어 있다
    #[test]
    fn builtin_catalog() {
        let v: Value = serde_json::from_str(BUILTIN).unwrap();
        assert!(valid_catalog(&v));
        let base = v["models"].as_array().unwrap().iter().find(|m| m["alias"] == BASE_ALIAS).unwrap();
        assert_eq!(base["builtin"], true);
        assert_eq!(base["file"], BASE_FILE);
    }
}
