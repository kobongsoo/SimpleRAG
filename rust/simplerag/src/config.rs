//------------------------------------------------------------------
// 전역 설정/기본값 + config.yaml 읽기·검증 (Python config.py · settings.py)
//=> 우선순위는 Python 판과 같다: 환경변수 > config.yaml > 코드 기본값.
//   설정 파일 찾는 순서: SIMPLERAG_CONFIG → <데이터 루트>/config.yaml → <프로그램 폴더>/config.yaml
//   (SIMPLERAG_CONFIG=- 이면 파일을 쓰지 않는다)
//   모르는 섹션·키, 잘못된 타입, 범위 밖 값은 ConfigError 로 멈춘다 — 오타를 조용히 무시하면
//   기본값으로 수십 분짜리 인덱싱이 다 돈 뒤에야 설정이 안 먹었음을 알게 된다.
//
//   데이터 루트: SIMPLERAG_HOME 이 있으면 그 폴더, 없으면 exe 가 있는 폴더(Python exe 판과 같다).
//------------------------------------------------------------------

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde_yaml::Value as Y;

use crate::pyutil;

//------------------------------------------------------------------
// 설정 오류
// -필드: 0 = 사람이 읽을 오류문(무엇을 고칠지)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

// config.yaml 의 값 하나(검사 후 정규화된 모양)
#[derive(Debug, Clone, PartialEq)]
pub enum SVal {
    Int(i64),
    Num(f64),
    Str(String),
    Bool(bool),
}

//------------------------------------------------------------------
// 생성 모델 표 — 별칭 → (GGUF 파일, 채팅 형식)
//------------------------------------------------------------------
pub const GEN_MODELS: &[(&str, &str, &str)] = &[
    ("qwen3-0.6b-q4", "Qwen3-0.6B-Q4_K_M.gguf", "qwen3"),
    ("qwen3-1.7b-q4", "Qwen3-1.7B-Q4_K_M.gguf", "qwen3"),
    ("midm-2.0-mini-q4", "Midm-2.0-Mini-Instruct-Q4_K_M.gguf", "midm"),
    ("kanana-1.5-2.1b-q4", "kanana-1.5-2.1b-instruct-Q4_K_M.gguf", "kanana"),
];
pub const FAST_GEN_MODEL: &str = "qwen3-0.6b-q4";
pub const PRECISE_GEN_MODEL: &str = "qwen3-1.7b-q4";
pub const KOREAN_GEN_MODEL: &str = "midm-2.0-mini-q4";
pub const USER_GEN_MODELS: &[&str] = &[FAST_GEN_MODEL, PRECISE_GEN_MODEL, KOREAN_GEN_MODEL];

// 고정 상수(Python config.py 와 같은 값 — 실측 근거는 원본 주석 참고)
pub const EMBED_DIM: usize = 384;
pub const PASSAGE_PREFIX: &str = "passage: ";
pub const QUERY_PREFIX: &str = "query: ";
pub const EMBED_MAX_TOKENS: usize = 512;
pub const EMBED_THREADS: usize = 4;
pub const EMBED_BATCH: usize = 8;
pub const CHUNKER_VERSION: i64 = 2;
pub const RERANK_MAX_TOKENS: usize = 512;
pub const GEN_THREADS: i32 = 8;
pub const GEN_N_CTX: u32 = 2048;
pub const GEN_N_BATCH: u32 = 512;
pub const GEN_FLASH_ATTN: bool = true;
pub const GEN_FLASH_ATTN_GPU: bool = false;
pub const GEN_GPU_LAYERS: i32 = -1;
pub const GEN_TEMPERATURE: f64 = 0.0;
pub const GPU_MIN_SPEEDUP: f64 = 1.3;
pub const BACKEND_PROBE_TIMEOUT: u64 = 180;
pub const CHECKPOINT_EVERY: usize = 500;
pub const BM25_MODE: &str = "subword";

//------------------------------------------------------------------
// 최종 설정값 묶음
// -필드: root / app_dir     = 데이터 루트 / 프로그램 폴더
// -필드: config_path        = 읽은 config.yaml (없으면 None)
// -필드: 나머지             = Python config.py 의 같은 이름 상수
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Config {
    pub root: String,
    pub app_dir: String,
    pub config_path: Option<String>,
    pub warnings: Vec<String>,
    pub model_dir: String,
    pub models_dir: String,
    pub index_dir: String,
    pub state_path: String,
    pub bm25_path: String,
    pub chunk_tokens: i64,
    pub chunk_overlap: i64,
    pub min_chunk_tokens: i64,
    pub table_header_max_tokens: i64,
    pub chunk_mode: String,
    pub extract_engine: String,
    pub pdf_infer_headings: bool,
    pub extract_dump: bool,
    pub chunk_overflow_ratio: f64,
    pub chunk_sentence_split: bool,
    pub chunk_hard_max: i64,
    pub top_k: i64,
    pub dense_top_k: i64,
    pub bm25_top_k: i64,
    pub dedup_evidence: bool,
    pub rrf_k: f64,
    pub bm25_k1: f64,
    pub bm25_b: f64,
    pub rerank: String,
    pub rerank_pool: i64,
    pub rerank_dir: String,
    pub rerank_defer_load: bool,
    pub default_gen_model: String,
    pub gen_max_tokens: i64,
    pub prompt: String,
    pub system_prompt_text: String,
    pub flatten_tables: bool,
    // 지시문 되뱉기 거르기(generation.echo_filter, 기본 끔) — 켜면 화면 첫 글자가 약 2초 늦어진다
    pub echo_filter: bool,
    pub parent_mode: String,
    pub parent_max_tokens: i64,
    pub gen_backend: String,
    pub vulkan_lib_dir: String,
    pub cpu_lib_dir: String,
    pub backend_cache_path: String,
    pub cso_snf: Option<String>,
}

static CONFIG: OnceLock<Config> = OnceLock::new();

//------------------------------------------------------------------
// 설정 적재 (프로그램 시작 때 한 번)
//=> main 이 가장 먼저 부른다. 오류면 main 이 "설정 오류: …" 로 알리고 끝낸다.
//
// -in: 없음
//
// -out: Ok(&Config) / Err(ConfigError)
// -out: error = config.yaml 오류·환경변수 형식 오류·값 모순이면 ConfigError
//------------------------------------------------------------------
pub fn init() -> Result<&'static Config, ConfigError> {
    if let Some(c) = CONFIG.get() {
        return Ok(c);
    }
    let c = build()?;
    let _ = CONFIG.set(c);
    Ok(CONFIG.get().unwrap())
}

//------------------------------------------------------------------
// 적재된 설정 꺼내기 (init 뒤에만)
//------------------------------------------------------------------
pub fn cfg() -> &'static Config {
    CONFIG.get_or_init(|| build().unwrap_or_else(|e| panic!("설정 오류: {}", e)))
}

// 환경변수 하나(빈 값은 없는 것으로)
fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

//------------------------------------------------------------------
// 설정값 하나 고르기 — 환경변수 > yaml > 기본값 (정수)
//------------------------------------------------------------------
fn pick_int(y: &BTreeMap<String, SVal>, key: &str, def: i64, env_name: Option<&str>) -> Result<i64, ConfigError> {
    if let Some(n) = env_name {
        if let Some(raw) = env(n) {
            return raw.trim().parse::<i64>().map_err(|_| {
                ConfigError(format!("환경변수 {}={:?} 을(를) 읽을 수 없습니다", n, raw))
            });
        }
    }
    Ok(match y.get(key) {
        Some(SVal::Int(i)) => *i,
        Some(SVal::Num(f)) => *f as i64,
        _ => def,
    })
}

// 실수판
fn pick_num(y: &BTreeMap<String, SVal>, key: &str, def: f64, env_name: Option<&str>) -> Result<f64, ConfigError> {
    if let Some(n) = env_name {
        if let Some(raw) = env(n) {
            return raw.trim().parse::<f64>().map_err(|_| {
                ConfigError(format!("환경변수 {}={:?} 을(를) 읽을 수 없습니다", n, raw))
            });
        }
    }
    Ok(match y.get(key) {
        Some(SVal::Num(f)) => *f,
        Some(SVal::Int(i)) => *i as f64,
        _ => def,
    })
}

// 문자열판
fn pick_str(y: &BTreeMap<String, SVal>, key: &str, def: &str, env_name: Option<&str>) -> String {
    if let Some(n) = env_name {
        if let Some(raw) = env(n) {
            return raw;
        }
    }
    match y.get(key) {
        Some(SVal::Str(s)) => s.clone(),
        _ => def.to_string(),
    }
}

// 참/거짓판(yaml 만)
fn pick_bool(y: &BTreeMap<String, SVal>, key: &str, def: bool) -> bool {
    match y.get(key) {
        Some(SVal::Bool(b)) => *b,
        _ => def,
    }
}

//------------------------------------------------------------------
// 최종 설정 조립 (핵심)
//=> Python config.py 를 위에서 아래로 그대로 옮겼다.
//------------------------------------------------------------------
fn build() -> Result<Config, ConfigError> {
    let app_dir = pyutil::exe_dir().to_string_lossy().into_owned();
    let root = match env("SIMPLERAG_HOME") {
        Some(h) => pyutil::abspath(&h),
        None => app_dir.clone(),
    };
    let (config_path, y) = load_settings(&root, &app_dir)?;

    // 임베딩 모델 폴더 — 환경변수 > <ROOT>/models/e5-small-ko > 형제 프로젝트
    let model_dir = match env("SIMPLERAG_EMBED_DIR") {
        Some(d) => pyutil::abspath(&d),
        None => {
            let local = pyutil::join(&pyutil::join(&root, "models"), "e5-small-ko");
            if std::path::Path::new(&pyutil::join(&local, "model.onnx")).is_file() {
                local
            } else {
                sibling_path(r"resources\models\e5-small-ko")
            }
        }
    };
    let models_dir = env("SIMPLERAG_MODELS_DIR").unwrap_or_else(|| pyutil::join(&root, "models"));

    let chunk_tokens = pick_int(&y, "chunk.tokens", 128, Some("SIMPLERAG_CHUNK_TOKENS"))?;
    let chunk_overlap = pick_int(&y, "chunk.overlap", std::cmp::max(8, chunk_tokens / 8), Some("SIMPLERAG_CHUNK_OVERLAP"))?;
    let min_chunk_tokens = pick_int(&y, "chunk.min_tokens", 16, None)?;
    let table_header_max_tokens = pick_int(&y, "chunk.table_header_max_tokens", 64, None)?;
    let chunk_mode = pick_str(&y, "chunk.mode", "structured", Some("SIMPLERAG_CHUNK_MODE"));

    let extract_engine = pick_str(&y, "extract.engine", "legacy", Some("SIMPLERAG_EXTRACT_ENGINE")).trim().to_lowercase();
    if extract_engine != "legacy" && extract_engine != "blocks" {
        return Err(ConfigError(format!(
            "SIMPLERAG_EXTRACT_ENGINE={:?} — legacy | blocks 중 하나여야 합니다", extract_engine)));
    }
    let pdf_infer_headings = match env("SIMPLERAG_PDF_HEADINGS") {
        Some(s) => !["0", "false", "no", ""].contains(&s.trim()),
        None => pick_bool(&y, "extract.pdf_headings", false),
    };
    let extract_dump = pick_bool(&y, "extract.dump_blocks", false);
    let chunk_overflow_ratio = pick_num(&y, "chunk.overflow_ratio", 1.0, Some("SIMPLERAG_CHUNK_OVERFLOW"))?;
    let chunk_sentence_split = pick_bool(&y, "chunk.sentence_split", false);
    let chunk_hard_max = pick_int(&y, "chunk.hard_max", 510, None)?;

    let top_k = pick_int(&y, "generation.top_k", 3, None)?;
    let dense_top_k = pick_int(&y, "retrieval.dense_top_k", 10, None)?;
    let bm25_top_k = pick_int(&y, "retrieval.bm25_top_k", 10, None)?;
    let dedup_evidence = match env("SIMPLERAG_DEDUP") {
        Some(v) => v != "0",
        None => pick_bool(&y, "retrieval.dedup", true),
    };
    let rrf_k = pick_num(&y, "retrieval.rrf_k", 60.0, None)?;
    let bm25_k1 = pick_num(&y, "retrieval.bm25_k1", 1.5, None)?;
    let bm25_b = pick_num(&y, "retrieval.bm25_b", 0.75, None)?;

    let rerank = match env("SIMPLERAG_RERANK").map(|s| s.trim().to_lowercase()).filter(|s| !s.is_empty()) {
        Some(s) => s,
        None => pick_str(&y, "rerank.mode", "auto", None),
    };
    let rerank_pool = pick_int(&y, "rerank.pool", 10, None)?;
    let rerank_dir = env("SIMPLERAG_RERANK_DIR").unwrap_or_else(|| pyutil::join(&models_dir, "bge-reranker-base-int8"));
    let rerank_defer_load = std::env::var("SIMPLERAG_RERANK_DEFER").map(|v| v != "0").unwrap_or(true);

    let warnings = check_resolved(chunk_tokens, chunk_overlap, min_chunk_tokens, table_header_max_tokens,
                                  top_k, rerank_pool, dense_top_k, bm25_top_k)?;

    let default_gen_model = pick_str(&y, "generation.model", PRECISE_GEN_MODEL, Some("SIMPLERAG_GEN_MODEL")).trim().to_lowercase();
    if gen_file(&default_gen_model).is_none() {
        return Err(ConfigError(format!(
            "SIMPLERAG_GEN_MODEL={:?} — {} 중 하나여야 합니다", default_gen_model,
            GEN_MODELS.iter().map(|m| m.0).collect::<Vec<_>>().join(" | "))));
    }
    let gen_max_tokens = pick_int(&y, "generation.max_tokens", 200, Some("SIMPLERAG_GEN_MAX_TOKENS"))?;
    let prompt = pick_str(&y, "generation.prompt", "v0", Some("SIMPLERAG_PROMPT")).trim().to_lowercase();
    if !["v0", "v4", "v5", "custom"].contains(&prompt.as_str()) {
        return Err(ConfigError(format!(
            "SIMPLERAG_PROMPT={:?} — v0 | v4 | v5 | custom 중 하나여야 합니다", prompt)));
    }
    let system_prompt_text = pyutil::strip(&pick_str(&y, "generation.system_prompt", "", None)).to_string();
    if prompt == "custom" && system_prompt_text.is_empty() {
        return Err(ConfigError(
            "generation.prompt: custom 이면 generation.system_prompt 에 지시문을 적어야 합니다".into()));
    }
    let flatten_tables = match env("SIMPLERAG_FLATTEN_TABLES") {
        Some(v) => v != "0",
        None => pick_bool(&y, "generation.flatten_tables", false),
    };
    // 지시문 되뱉기 거르기 — 기본 끔. 켜면 스트리밍 출력기가 되뱉기인지 판정하려고 첫 문장(최대 56자)을
    //   붙들어 화면 첫 글자가 TTFT 보다 약 2초 늦어진다(골든셋 100문항 실측 2026-10-26). Python 판과 같은 규칙
    let echo_filter = match env("SIMPLERAG_ECHO_FILTER") {
        Some(v) => v != "0",
        None => pick_bool(&y, "generation.echo_filter", false),
    };
    let parent_mode = pick_str(&y, "generation.parent", "off", Some("SIMPLERAG_PARENT")).trim().to_lowercase();
    if parent_mode != "off" && parent_mode != "section" {
        return Err(ConfigError(format!(
            "SIMPLERAG_PARENT={:?} — off | section 중 하나여야 합니다", parent_mode)));
    }
    let parent_max_tokens = pick_int(&y, "generation.parent_max_tokens", 400, Some("SIMPLERAG_PARENT_MAX_TOKENS"))?;

    let gen_backend = std::env::var("SIMPLERAG_GEN_BACKEND").unwrap_or_else(|_| "auto".into()).trim().to_lowercase();
    // Vulkan / CPU 판 llama.cpp DLL 폴더(프로그램 폴더 기준) — 데이터 루트를 옮겨도 iGPU 가 꺼지지 않게
    let vulkan_lib_dir = env("SIMPLERAG_VULKAN_LIB")
        .unwrap_or_else(|| pyutil::join(&pyutil::join(&app_dir, "runtime"), "llama_vulkan"));
    let cpu_lib_dir = env("SIMPLERAG_CPU_LIB")
        .unwrap_or_else(|| pyutil::join(&pyutil::join(&app_dir, "runtime"), "llama_cpu"));

    Ok(Config {
        index_dir: pyutil::join(&root, "rag_index"),
        state_path: pyutil::join(&root, "index_state.json"),
        bm25_path: pyutil::join(&root, "bm25_index.bin"),
        backend_cache_path: pyutil::join(&root, "gen_backend.json"),
        root,
        app_dir,
        config_path,
        warnings,
        model_dir,
        models_dir,
        chunk_tokens,
        chunk_overlap,
        min_chunk_tokens,
        table_header_max_tokens,
        chunk_mode,
        extract_engine,
        pdf_infer_headings,
        extract_dump,
        chunk_overflow_ratio,
        chunk_sentence_split,
        chunk_hard_max,
        top_k,
        dense_top_k,
        bm25_top_k,
        dedup_evidence,
        rrf_k,
        bm25_k1,
        bm25_b,
        rerank,
        rerank_pool,
        rerank_dir,
        rerank_defer_load,
        default_gen_model,
        gen_max_tokens,
        prompt,
        system_prompt_text,
        flatten_tables,
        echo_filter,
        parent_mode,
        parent_max_tokens,
        gen_backend,
        vulkan_lib_dir,
        cpu_lib_dir,
        cso_snf: env("SIMPLERAG_SNF_EXE"),
    })
}

// 형제 프로젝트 후보(MpowerClassify → 옛 이름 CSOClassify)
fn sibling_path(sub: &str) -> String {
    for root in [r"D:\Project\MpowerClassify", r"D:\Project\CSOClassify"] {
        let cand = pyutil::join(root, sub);
        if std::path::Path::new(&cand).exists() {
            return cand;
        }
    }
    pyutil::join(r"D:\Project\MpowerClassify", sub)
}

//------------------------------------------------------------------
// 별칭 → GGUF 파일 이름
//------------------------------------------------------------------
pub fn gen_file(alias: &str) -> Option<&'static str> {
    GEN_MODELS.iter().find(|m| m.0 == alias).map(|m| m.1)
}

//------------------------------------------------------------------
// 별칭 → 채팅 형식 이름(없으면 qwen3)
//------------------------------------------------------------------
pub fn gen_format(alias: &str) -> &'static str {
    GEN_MODELS.iter().find(|m| m.0 == alias).map(|m| m.2).unwrap_or("qwen3")
}

//------------------------------------------------------------------
// 생성 모델 파일 경로
//=> <models_dir>/<파일>. 없으면 프로그램 폴더의 models 와 옆 Python 배포본(..\simplerag\models)도
//   찾아본다 — 수 GB 모델을 두 벌 두지 않고도 Rust 판을 시험할 수 있게.
//------------------------------------------------------------------
pub fn gen_model_path(alias: &str) -> String {
    let c = cfg();
    let file = gen_file(alias).unwrap_or(alias);
    let main = pyutil::join(&c.models_dir, file);
    if std::path::Path::new(&main).is_file() {
        return main;
    }
    for d in [pyutil::join(&c.app_dir, "models"),
              pyutil::join(&pyutil::join(&pyutil::dirname(&c.app_dir), "simplerag"), "models"),
              pyutil::join(&pyutil::join(&pyutil::join(&pyutil::dirname(&c.app_dir), "dist"), "simplerag"), "models")] {
        let p = pyutil::join(&d, file);
        if std::path::Path::new(&p).is_file() {
            return p;
        }
    }
    main
}

// ── config.yaml 스키마 검사 (settings.py) ─────────────────

// 검사기 종류
enum Check {
    Int(i64, i64),
    OptInt(i64, i64),
    Num(f64, f64, bool),
    Choice(&'static [&'static str]),
    Bool,
    RerankMode,
    Text(usize),
    ParentMode,
}

// 섹션 → 키 → 검사기. 여기에 없는 키는 오타로 보고 오류를 낸다
fn schema() -> Vec<(&'static str, Vec<(&'static str, Check)>)> {
    vec![
        ("chunk", vec![
            ("tokens", Check::Int(16, 510)),
            ("overlap", Check::OptInt(0, 509)),
            ("mode", Check::Choice(&["structured", "fixed"])),
            ("min_tokens", Check::Int(1, 510)),
            ("table_header_max_tokens", Check::Int(0, 510)),
            ("overflow_ratio", Check::Num(1.0, 4.0, false)),
            ("sentence_split", Check::Bool),
            ("hard_max", Check::Int(16, 510)),
        ]),
        ("extract", vec![
            ("engine", Check::Choice(&["legacy", "blocks"])),
            ("dump_blocks", Check::Bool),
            ("pdf_headings", Check::Bool),
        ]),
        ("retrieval", vec![
            ("dense_top_k", Check::Int(1, 1000)),
            ("bm25_top_k", Check::Int(0, 1000)),
            ("rrf_k", Check::Num(0.0, 100000.0, true)),
            ("bm25_k1", Check::Num(0.0, 100.0, true)),
            ("bm25_b", Check::Num(0.0, 1.0, false)),
            ("dedup", Check::Bool),
        ]),
        ("rerank", vec![
            ("mode", Check::RerankMode),
            ("pool", Check::Int(1, 100)),
        ]),
        ("generation", vec![
            ("top_k", Check::Int(1, 20)),
            ("max_tokens", Check::Int(16, 1024)),
            ("prompt", Check::Choice(&["v0", "v4", "v5", "custom"])),
            ("system_prompt", Check::Text(1000)),
            ("model", Check::Choice(&["qwen3-0.6b-q4", "qwen3-1.7b-q4", "midm-2.0-mini-q4"])),
            ("flatten_tables", Check::Bool),
            ("echo_filter", Check::Bool),
            ("parent", Check::ParentMode),
            ("parent_max_tokens", Check::Int(64, 1500)),
        ]),
    ]
}

//------------------------------------------------------------------
// YAML 1.1 참/거짓 글자 — PyYAML 은 yes/no/on/off 도 bool 로 읽는다(serde_yaml 은 YAML 1.2)
//------------------------------------------------------------------
fn yaml11_bool(v: &Y) -> Option<bool> {
    match v {
        Y::Bool(b) => Some(*b),
        Y::String(s) => match s.as_str() {
            "yes" | "Yes" | "YES" | "on" | "On" | "ON" | "true" | "True" | "TRUE" | "y" | "Y" => Some(true),
            "no" | "No" | "NO" | "off" | "Off" | "OFF" | "false" | "False" | "FALSE" | "n" | "N" => Some(false),
            _ => None,
        },
        _ => None,
    }
}

// YAML 값을 오류문에 쓸 글자로(파이썬 str(v) 흉내)
fn yshow(v: &Y) -> String {
    match v {
        Y::Bool(b) => if *b { "True".into() } else { "False".into() },
        Y::Number(n) => n.to_string(),
        Y::String(s) => s.clone(),
        Y::Null => "None".into(),
        other => serde_yaml::to_string(other).unwrap_or_default().trim().to_string(),
    }
}

//------------------------------------------------------------------
// 값 하나 검사 — (정규화된 값 또는 None, 오류문)
//------------------------------------------------------------------
fn run_check(ch: &Check, v: &Y) -> Result<Option<SVal>, String> {
    match ch {
        Check::Int(lo, hi) | Check::OptInt(lo, hi) => {
            if let Check::OptInt(..) = ch {
                if v.is_null() {
                    return Ok(None);
                }
            }
            match v {
                Y::Number(n) if n.is_i64() => {
                    let i = n.as_i64().unwrap();
                    if i < *lo || i > *hi {
                        Err(format!("{}~{} 사이여야 합니다", lo, hi))
                    } else {
                        Ok(Some(SVal::Int(i)))
                    }
                }
                _ => Err("정수여야 합니다".into()),
            }
        }
        Check::Num(lo, hi, lo_open) => {
            let f = match v {
                Y::Number(n) => n.as_f64().unwrap_or(0.0),
                _ => return Err("숫자여야 합니다".into()),
            };
            let bad_lo = if *lo_open { f <= *lo } else { f < *lo };
            if bad_lo || f > *hi {
                Err(format!("{} {} ~ {} 이하여야 합니다", fmt_g(*lo), if *lo_open { "초과" } else { "이상" }, fmt_g(*hi)))
            } else {
                Ok(Some(SVal::Num(f)))
            }
        }
        Check::Choice(choices) => {
            let s = pyutil::strip(&yshow(v)).to_lowercase();
            if choices.contains(&s.as_str()) {
                Ok(Some(SVal::Str(s)))
            } else {
                Err(format!("{} 중 하나여야 합니다", choices.join(" | ")))
            }
        }
        Check::Bool => match yaml11_bool(v) {
            Some(b) => Ok(Some(SVal::Bool(b))),
            None => Err("true 또는 false 여야 합니다".into()),
        },
        Check::RerankMode => {
            if let Y::Bool(b) = v {
                return Ok(Some(SVal::Str(if *b { "1" } else { "0" }.into())));
            }
            let s = pyutil::strip(&yshow(v)).to_lowercase();
            match s.as_str() {
                "auto" => Ok(Some(SVal::Str("auto".into()))),
                "on" | "1" | "true" | "yes" => Ok(Some(SVal::Str("1".into()))),
                "off" | "0" | "false" | "no" => Ok(Some(SVal::Str("0".into()))),
                _ => Err("auto | on | off 중 하나여야 합니다".into()),
            }
        }
        Check::Text(max) => match v {
            Y::String(s) => {
                let t = pyutil::strip(s);
                let n = pyutil::clen(t);
                if n > *max {
                    Err(format!("{}자 이하여야 합니다(지금 {}자)", max, n))
                } else {
                    Ok(Some(SVal::Str(t.to_string())))
                }
            }
            _ => Err("글(문자열)이어야 합니다".into()),
        },
        Check::ParentMode => {
            if let Y::Bool(false) = v {
                return Ok(Some(SVal::Str("off".into())));
            }
            let s = pyutil::strip(&yshow(v)).to_lowercase();
            if s == "off" || s == "section" {
                Ok(Some(SVal::Str(s)))
            } else {
                Err("off | section 중 하나여야 합니다".into())
            }
        }
    }
}

// 파이썬 "{}".format(float) 처럼 정수면 소수점 없이
fn fmt_g(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e15 {
        format!("{}", f as i64)
    } else {
        format!("{}", f)
    }
}

//------------------------------------------------------------------
// 설정 파일 찾기
//------------------------------------------------------------------
fn find_config(root: &str, app_dir: &str) -> Result<Option<String>, ConfigError> {
    if let Ok(e) = std::env::var("SIMPLERAG_CONFIG") {
        let e = e.trim().to_string();
        if !e.is_empty() {
            if e == "-" {
                return Ok(None);
            }
            let p = pyutil::abspath(&e);
            if !std::path::Path::new(&p).is_file() {
                return Err(ConfigError(format!("SIMPLERAG_CONFIG 로 지정한 파일이 없습니다: {}", p)));
            }
            return Ok(Some(p));
        }
    }
    for d in [root, app_dir] {
        let p = pyutil::join(d, "config.yaml");
        if std::path::Path::new(&p).is_file() {
            return Ok(Some(p));
        }
    }
    Ok(None)
}

//------------------------------------------------------------------
// config.yaml 읽기 (핵심)
//    1) 파일을 찾는다(없으면 빈 설정)
//    2) UTF-8(BOM 허용)으로 읽어 YAML 로 푼다
//    3) 섹션·키·값을 스키마로 검사하고, 오류를 모두 모아 한 번에 알린다
//
// -out: (경로, {"섹션.키": 값}) — null 값은 넣지 않는다(코드 기본값을 쓴다)
// -out: error = 형식 오류·모르는 키·타입/범위 오류는 ConfigError(파일 경로 포함)
//------------------------------------------------------------------
pub fn load_settings(root: &str, app_dir: &str) -> Result<(Option<String>, BTreeMap<String, SVal>), ConfigError> {
    let path = match find_config(root, app_dir)? {
        Some(p) => p,
        None => return Ok((None, BTreeMap::new())),
    };
    let raw = std::fs::read(&path).map_err(|e| ConfigError(format!("{} — 읽을 수 없습니다: {}", path, e)))?;
    let text = String::from_utf8_lossy(&raw);
    let text = text.trim_start_matches('\u{feff}');
    let data: Y = serde_yaml::from_str(text).map_err(|e| ConfigError(format!("{} — YAML 형식 오류: {}", path, e)))?;
    let mut flat = BTreeMap::new();
    let map = match data {
        Y::Null => return Ok((Some(path), flat)),
        Y::Mapping(m) => m,
        _ => {
            return Err(ConfigError(format!(
                "{} — 최상위에는 섹션(chunk / retrieval / rerank / generation)이 와야 합니다", path)))
        }
    };
    let sch = schema();
    let mut errors = vec![];
    for (sec, body) in map.iter() {
        let sec_name = yshow(sec);
        let spec = match sch.iter().find(|(n, _)| *n == sec_name) {
            Some((_, s)) => s,
            None => {
                let names: Vec<&str> = sch.iter().map(|(n, _)| *n).collect();
                errors.push(format!("알 수 없는 섹션 '{}'{}", sec_name, hint(&sec_name, &names)));
                continue;
            }
        };
        let body = match body {
            Y::Null => continue,
            Y::Mapping(m) => m,
            _ => {
                errors.push(format!("'{}' 아래에는 '키: 값' 줄이 와야 합니다", sec_name));
                continue;
            }
        };
        for (k, v) in body.iter() {
            let key = yshow(k);
            let ch = match spec.iter().find(|(n, _)| *n == key) {
                Some((_, c)) => c,
                None => {
                    let names: Vec<&str> = spec.iter().map(|(n, _)| *n).collect();
                    errors.push(format!("알 수 없는 키 '{}.{}'{}", sec_name, key, hint(&key, &names)));
                    continue;
                }
            };
            if v.is_null() {
                continue;
            }
            match run_check(ch, v) {
                Ok(Some(n)) => {
                    flat.insert(format!("{}.{}", sec_name, key), n);
                }
                Ok(None) => {}
                Err(e) => errors.push(format!("'{}.{}: {}' — {}", sec_name, key, yshow(v), e)),
            }
        }
    }
    if !errors.is_empty() {
        return Err(ConfigError(format!("{}\n  - {}", path, errors.join("\n  - "))));
    }
    Ok((Some(path), flat))
}

//------------------------------------------------------------------
// 오타 힌트 — difflib.get_close_matches(n=1, cutoff=0.6) 와 같은 유사도
//------------------------------------------------------------------
fn hint(name: &str, options: &[&str]) -> String {
    let mut best: Option<(f64, &str)> = None;
    for o in options {
        let r = seq_ratio(name, o);
        if r >= 0.6 && best.map(|b| r > b.0).unwrap_or(true) {
            best = Some((r, o));
        }
    }
    match best {
        Some((_, o)) => format!(" (혹시 '{}'?)", o),
        None => String::new(),
    }
}

// difflib.SequenceMatcher.ratio() — 가장 긴 공통 조각을 재귀로 모아 2*M/T
fn seq_ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }
    fn matches(a: &[char], b: &[char]) -> usize {
        let (mut bi, mut bj, mut bk) = (0, 0, 0);
        for i in 0..a.len() {
            for j in 0..b.len() {
                let mut k = 0;
                while i + k < a.len() && j + k < b.len() && a[i + k] == b[j + k] {
                    k += 1;
                }
                if k > bk {
                    bi = i;
                    bj = j;
                    bk = k;
                }
            }
        }
        if bk == 0 {
            return 0;
        }
        bk + matches(&a[..bi], &b[..bj]) + matches(&a[bi + bk..], &b[bj + bk..])
    }
    2.0 * matches(&a, &b) as f64 / total as f64
}

//------------------------------------------------------------------
// 조립된 최종값끼리의 모순 검사
// -out: 경고 문장 목록 / error = 고칠 수밖에 없는 모순은 ConfigError
//------------------------------------------------------------------
#[allow(clippy::too_many_arguments)]
fn check_resolved(tokens: i64, overlap: i64, min_tokens: i64, header_max: i64, top_k: i64, pool: i64,
                  dense_k: i64, bm25_k: i64) -> Result<Vec<String>, ConfigError> {
    let mut errors = vec![];
    if !(16..=510).contains(&tokens) {
        errors.push(format!("청크 크기 {} — 16~510 이어야 합니다", tokens));
    }
    if !(0 <= overlap && overlap < tokens) {
        errors.push(format!("겹침 {} — 0 이상, 청크 크기({}) 미만이어야 합니다", overlap, tokens));
    }
    if min_tokens > tokens {
        errors.push(format!("최소 조각 {} — 청크 크기({}) 이하여야 합니다", min_tokens, tokens));
    }
    if !errors.is_empty() {
        return Err(ConfigError(format!("설정값 모순\n  - {}", errors.join("\n  - "))));
    }
    let mut w = vec![];
    if pool < top_k {
        w.push(format!("rerank.pool({}) < generation.top_k({}) — 리랭커 입력은 top_k 로 올려서 쓴다(재정렬 여지 없음)", pool, top_k));
    }
    if dense_k + bm25_k < pool.max(top_k) {
        w.push(format!("dense_top_k + bm25_top_k({}) < 필요한 후보 수({}) — 근거가 모자랄 수 있다", dense_k + bm25_k, pool.max(top_k)));
    }
    if header_max > tokens {
        w.push(format!("table_header_max_tokens({}) > 청크 크기({}) — 표 청크가 크기의 두 배 넘게 커질 수 있다", header_max, tokens));
    }
    Ok(w)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ratio_hint() {
        assert_eq!(hint("token", &["tokens", "mode"]), " (혹시 'tokens'?)");
        assert_eq!(hint("zzz", &["tokens"]), "");
    }
}
