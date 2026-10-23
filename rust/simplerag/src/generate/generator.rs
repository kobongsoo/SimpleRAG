//------------------------------------------------------------------
// GGUF 생성기 (Python generate/gguf_generator.py + llama-cpp-python create_completion)
//=> 근거와 질문으로 프롬프트를 만들어 llama.dll 로 답을 흘려보낸다.
//    · 온도 0 → 매 단계 가장 큰 logit 토큰(탐욕 선택)
//    · 프롬프트 앞부분이 이전과 같으면 KV 를 재사용(시스템 지시문)
//    · 중단: 턴 끝 토큰(EOG) · 중단 문자열 · max_tokens
//    · 스트리밍은 중단 문자열의 앞부분일 수 있는 꼬리와 깨진 UTF-8 은 붙들었다 내보낸다
//------------------------------------------------------------------

use std::sync::Mutex;
use std::time::Instant;

use super::llama::{self, LoadOpts, Model};
use super::prompts::{active_system_prompt, build_chat_prompt, build_user_message, stop_strings};
use crate::config::{self, cfg};
use crate::pyutil;

pub struct Generator {
    pub alias: String,
    pub backend: Mutex<String>,
    pub last_warm_ms: Mutex<Option<f64>>,
    model: Mutex<Option<Model>>,
    load_lock: Mutex<()>,
}

impl Generator {
    pub fn new(alias: Option<&str>) -> Generator {
        Generator {
            alias: alias.map(|s| s.to_string()).unwrap_or_else(|| cfg().default_gen_model.clone()),
            backend: Mutex::new("cpu".into()),
            last_warm_ms: Mutex::new(None),
            model: Mutex::new(None),
            load_lock: Mutex::new(()),
        }
    }

    pub fn is_ready(&self) -> bool {
        self.model.lock().unwrap().is_some()
    }

    pub fn backend(&self) -> String {
        self.backend.lock().unwrap().clone()
    }

    //------------------------------------------------------------------
    // 모델 적재 보장 — 걸린 ms
    //=> 백엔드(cpu|vulkan)에 맞는 DLL 폴더를 고르고 적재 인자를 정한다.
    //   iGPU 로 정했는데 적재된 DLL 이 GPU 를 못 쓰면 CPU 로 바로잡는다.
    //------------------------------------------------------------------
    pub fn ensure_loaded(&self) -> Result<f64, String> {
        if self.is_ready() {
            return Ok(0.0);
        }
        let _g = self.load_lock.lock().unwrap();
        if self.is_ready() {
            return Ok(0.0);
        }
        let path = config::gen_model_path(&self.alias);
        if !std::path::Path::new(&path).is_file() {
            return Err(format!("GenerateError: 생성 모델 없음: {}\n        bench/download_models.py 로 내려받으세요.", path));
        }
        let mut backend = self.backend();
        let dir = if backend == "vulkan" { cfg().vulkan_lib_dir.clone() } else { cfg().cpu_lib_dir.clone() };
        let api = llama::api_for(&dir).map_err(|e| format!("GenerateError: llama.dll 적재 실패: {}", e))?;
        if backend == "vulkan" && !api.supports_gpu_offload() {
            backend = "cpu".into();
            *self.backend.lock().unwrap() = backend.clone();
        }
        let gpu = backend == "vulkan";
        let opts = LoadOpts {
            n_ctx: config::GEN_N_CTX,
            n_batch: config::GEN_N_BATCH,
            n_threads: config::GEN_THREADS,
            n_gpu_layers: if gpu { config::GEN_GPU_LAYERS } else { 0 },
            flash_attn: if gpu { config::GEN_FLASH_ATTN_GPU } else { config::GEN_FLASH_ATTN },
        };
        let t0 = Instant::now();
        let m = Model::load(api, &path, &opts).map_err(|e| format!("GenerateError: 모델 로딩 실패: {}", e))?;
        *self.model.lock().unwrap() = Some(m);
        Ok(t0.elapsed().as_secs_f64() * 1000.0)
    }

    //------------------------------------------------------------------
    // 예열 생성 — 셰이더 컴파일·시스템 지시문 KV 를 미리 치른다(답은 버린다)
    //------------------------------------------------------------------
    pub fn warm(&self) -> Result<f64, String> {
        self.ensure_loaded()?;
        let t0 = Instant::now();
        let ev = vec!["예열용 근거 문장입니다.".to_string(); 3];
        self.stream("예열 질문입니다.", &ev, Some(1), None, &mut |_| {})?;
        let ms = pyutil::round_to(t0.elapsed().as_secs_f64() * 1000.0, 1);
        *self.last_warm_ms.lock().unwrap() = Some(ms);
        Ok(ms)
    }

    //------------------------------------------------------------------
    // 컨텍스트에 맞춰 프롬프트 만들기 — 넘치면 가장 긴 근거를 20%씩 깎는다
    //------------------------------------------------------------------
    fn fit_prompt(&self, m: &Model, system: &str, query: &str, chunks: &[String], max_tokens: usize) -> String {
        let budget = std::cmp::max(128, m.n_ctx as i64 - max_tokens as i64 - 16) as usize;
        let mut texts: Vec<String> = chunks.to_vec();
        let mut prompt = build_chat_prompt(system, &build_user_message(query, &texts), &self.alias);
        if pyutil::clen(&prompt) <= budget {
            return prompt;
        }
        for _ in 0..60 {
            let n_tok = m.tokenize(&prompt, false, true).len();
            if n_tok <= budget {
                return prompt;
            }
            // 가장 긴 근거(같으면 앞의 것)
            let mut i = 0;
            for k in 0..texts.len() {
                if pyutil::clen(&texts[k]) > pyutil::clen(&texts[i]) {
                    i = k;
                }
            }
            let cut = (pyutil::clen(&texts[i]) as f64 * 0.8) as usize;
            if cut < 40 {
                break;
            }
            texts[i] = pyutil::head(&texts[i], cut).to_string();
            prompt = build_chat_prompt(system, &build_user_message(query, &texts), &self.alias);
        }
        prompt
    }

    //------------------------------------------------------------------
    // 스트리밍 생성 (핵심) — 조각마다 on_piece 를 부른다
    //
    // -in: query, chunks = 질문·근거 / max_tokens = 답 토큰 상한(None=설정값)
    // -in: system = 지시문(None=설정) / on_piece = 조각 받는 함수
    //
    // -out: Ok(전체 답 글자)
    // -out: error = 적재·디코드 실패면 Err
    //------------------------------------------------------------------
    pub fn stream(&self, query: &str, chunks: &[String], max_tokens: Option<usize>, system: Option<&str>,
                  on_piece: &mut dyn FnMut(&str)) -> Result<String, String> {
        self.ensure_loaded()?;
        let mut g = self.model.lock().unwrap();
        let m = g.as_mut().unwrap();
        let n_out = max_tokens.unwrap_or(cfg().gen_max_tokens as usize);
        let sys = system.map(|s| s.to_string()).unwrap_or_else(active_system_prompt);
        let prompt = self.fit_prompt(m, &sys, query, chunks, n_out);
        let ptoks = m.prompt_tokens(&prompt);
        if ptoks.len() as u32 >= m.n_ctx {
            return Err(format!("ValueError: Requested tokens ({}) exceed context window of {}", ptoks.len(), m.n_ctx));
        }
        let mut max_new = n_out;
        if max_new == 0 || max_new + ptoks.len() > m.n_ctx as usize {
            max_new = m.n_ctx as usize - ptoks.len();
        }
        let stops: Vec<Vec<u8>> = stop_strings(&self.alias).into_iter().map(|s| s.as_bytes().to_vec()).collect();
        let mut all: Vec<u8> = vec![];
        let mut sent = 0usize;
        let mut n_gen = 0usize;
        let mut logits = m.eval_prompt(&ptoks)?;
        let mut stopped_at: Option<usize> = None;
        loop {
            // Ctrl+C — 이 답변만 취소한다
            if crate::cli::INTERRUPT.load(std::sync::atomic::Ordering::SeqCst) {
                return Err("KeyboardInterrupt".into());
            }
            let tok = llama::argmax(&logits);
            if m.is_eog(tok) {
                break;
            }
            all.extend_from_slice(&m.piece(tok));
            n_gen += 1;
            // 중단 문자열이 나왔으면 그 앞까지만
            if let Some(pos) = stops.iter().filter_map(|s| find_sub(&all, s)).min() {
                stopped_at = Some(pos);
                break;
            }
            // 중단 문자열의 앞부분일 수 있는 꼬리·깨진 UTF-8 은 붙들고 나머지를 내보낸다
            let hold = stops.iter().map(|s| suffix_prefix(&all, s)).max().unwrap_or(0);
            let safe = utf8_safe_end(&all[..all.len() - hold]);
            if safe > sent {
                let s = String::from_utf8_lossy(&all[sent..safe]).replace('\u{FFFD}', "");
                if !s.is_empty() {
                    on_piece(&s);
                }
                sent = safe;
            }
            if n_gen >= max_new {
                break;
            }
            logits = m.feed(&[tok])?;
        }
        let end = stopped_at.unwrap_or(all.len());
        if end > sent {
            // 남은 꼬리 — 깨진 바이트는 버린다(errors="ignore")
            let s = String::from_utf8_lossy(&all[sent..end]).replace('\u{FFFD}', "");
            if !s.is_empty() {
                on_piece(&s);
            }
        }
        Ok(String::from_utf8_lossy(&all[..end]).replace('\u{FFFD}', ""))
    }

    // 비스트리밍(일괄 평가용)
    pub fn generate(&self, query: &str, chunks: &[String], max_tokens: Option<usize>) -> Result<String, String> {
        self.stream(query, chunks, max_tokens, None, &mut |_| {})
    }
}

// 바이트 안에서 조각 찾기
fn find_sub(h: &[u8], n: &[u8]) -> Option<usize> {
    if n.is_empty() || h.len() < n.len() {
        return None;
    }
    h.windows(n.len()).position(|w| w == n)
}

// h 의 꼬리가 n 의 앞부분과 겹치는 가장 긴 길이
fn suffix_prefix(h: &[u8], n: &[u8]) -> usize {
    let max = n.len().saturating_sub(1).min(h.len());
    (1..=max).rev().find(|&k| h[h.len() - k..] == n[..k]).unwrap_or(0)
}

// 끝의 덜 끝난 UTF-8 글자를 뺀 길이
fn utf8_safe_end(b: &[u8]) -> usize {
    match std::str::from_utf8(b) {
        Ok(_) => b.len(),
        Err(e) => {
            if e.error_len().is_none() {
                e.valid_up_to()
            } else {
                b.len()
            }
        }
    }
}
