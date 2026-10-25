//------------------------------------------------------------------
// GGUF 생성기 (Python generate/gguf_generator.py + llama-cpp-python create_completion)
//=> 근거와 질문으로 프롬프트를 만들어 llama.dll 로 답을 흘려보낸다.
//    · 온도 0 → 매 단계 가장 큰 logit 토큰(탐욕 선택)
//    · 프롬프트 앞부분이 이전과 같으면 KV 를 재사용(시스템 지시문)
//    · 중단: 턴 끝 토큰(EOG) · 중단 문자열 · 반복 루프 · max_tokens
//    · 스트리밍은 중단 문자열의 앞부분일 수 있는 꼬리와 깨진 UTF-8 은 붙들었다 내보낸다
//    · 탐욕 선택은 같은 문장을 되풀이하는 루프에 빠질 수 있다(반복 벌점은 재현성 때문에 넣지 않는다).
//      앞에 나온 글과 같은 꼬리는 붙들어 두고, 그 길이가 LOOP_MIN_CHARS 에 이르면 반복으로 보고 멈춘다.
//    · max_tokens 에서 멈추면 마지막 완결 문장까지만 남기고 '(답변이 길어 일부 생략)' 표시를 단다
//      (상한 직전 TAIL_WINDOW 토큰 동안은 덜 끝난 문장을 붙들어 둔다)
//------------------------------------------------------------------

use std::sync::Mutex;
use std::time::Instant;

use super::llama::{self, LoadOpts, Model};
use super::prompts::{active_system_prompt, build_chat_prompt, build_user_message, stop_strings};
use crate::config::{self, cfg};
use crate::pyutil;

// 앞에 나온 글과 같은 꼬리가 이만큼(글자) 이어지면 반복 루프로 본다.
//   표 줄·"[1] 근거에 따르면," 같은 정상적인 짧은 되풀이는 이보다 짧다.
const LOOP_MIN_CHARS: usize = 40;
// 앞에 나온 글과 같은 꼬리가 이만큼(글자) 넘으면 반복일 수 있어 내보내지 않고 붙든다
const HOLD_MIN_CHARS: usize = 8;
// 상한까지 이만큼(토큰) 남으면 덜 끝난 문장을 붙든다 — 상한에서 잘릴 때 화면에 반쪽 문장이 남지 않게
const TAIL_WINDOW: usize = 48;
pub const NOTE_CAPPED: &str = "(답변이 길어 일부 생략)";
pub const NOTE_LOOP: &str = "(같은 내용이 반복되어 이하 생략)";

//------------------------------------------------------------------
// 생성 결과
// -필드: text = 답 글자(반복분·덜 끝난 문장을 잘라 낸 뒤)
// -필드: note = 잘라 냈다는 표시(없으면 None) — 본문과 따로 붙인다(인용 번호 정리에 끼지 않게)
//------------------------------------------------------------------
pub struct GenOut {
    pub text: String,
    pub note: Option<&'static str>,
}

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
    // -out: Ok(GenOut) = 답 글자 + 잘라 냈다는 표시(반복 루프 · 상한)
    // -out: error = 적재·디코드 실패면 Err
    //------------------------------------------------------------------
    pub fn stream(&self, query: &str, chunks: &[String], max_tokens: Option<usize>, system: Option<&str>,
                  on_piece: &mut dyn FnMut(&str)) -> Result<GenOut, String> {
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
        let mut guard = OutGuard::new(stop_strings(&self.alias));
        let mut n_gen = 0usize;
        let mut logits = m.eval_prompt(&ptoks)?;
        let mut halt: Option<Halt> = None;
        loop {
            // Ctrl+C — 이 답변만 취소한다
            if crate::cli::INTERRUPT.load(std::sync::atomic::Ordering::SeqCst) {
                return Err("KeyboardInterrupt".into());
            }
            let tok = llama::argmax(&logits);
            if m.is_eog(tok) {
                break;
            }
            n_gen += 1;
            // 중단 문자열 · 반복 루프가 나오면 멈춘다
            if let Some(h) = guard.push(&m.piece(tok), max_new - n_gen, on_piece) {
                halt = Some(h);
                break;
            }
            if n_gen >= max_new {
                // 아주 짧은 상한(예열 1토큰)은 일부러 그렇게 부른 것이라 자르지 않는다
                if max_new >= TAIL_WINDOW {
                    halt = Some(Halt::Capped);
                }
                break;
            }
            logits = m.feed(&[tok])?;
        }
        Ok(guard.finish(halt, on_piece))
    }

    // 비스트리밍(일괄 평가용)
    pub fn generate(&self, query: &str, chunks: &[String], max_tokens: Option<usize>) -> Result<String, String> {
        self.stream(query, chunks, max_tokens, None, &mut |_| {}).map(|o| o.text)
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

//------------------------------------------------------------------
// 생성을 멈춘 까닭(EOG 로 자연스럽게 끝나면 None)
//------------------------------------------------------------------
#[derive(Debug, PartialEq)]
enum Halt {
    Stop(usize), // 중단 문자열 — 그 앞(바이트 위치)까지
    Loop(usize), // 반복 루프 — 반복이 시작된 곳(바이트 위치) 앞까지
    Capped,      // max_tokens 에 닿았다
}

//------------------------------------------------------------------
// 출력 관리 — 생성된 바이트를 모으고, 내보내도 되는 앞부분만 흘려보낸다
//=> 화면에 나간 글은 되돌릴 수 없으므로, 나중에 잘라 낼 수 있는 꼬리는 붙들어 둔다.
//    1) 중단 문자열의 앞부분일 수 있는 꼬리
//    2) 앞에 나온 글과 같은 꼬리(HOLD_MIN_CHARS 이상) — 반복 루프의 시작일 수 있다
//    3) 상한까지 TAIL_WINDOW 토큰 안쪽이면 마지막 완결 문장 뒤의 덜 끝난 문장
//    4) 덜 끝난 UTF-8 글자
//
// -필드: all = 지금까지 생성된 바이트 / sent = 내보낸 바이트 수 / stops = 중단 문자열
//------------------------------------------------------------------
struct OutGuard {
    all: Vec<u8>,
    sent: usize,
    stops: Vec<Vec<u8>>,
}

impl OutGuard {
    fn new(stops: Vec<&str>) -> OutGuard {
        OutGuard { all: vec![], sent: 0, stops: stops.into_iter().map(|s| s.as_bytes().to_vec()).collect() }
    }

    //------------------------------------------------------------------
    // 조각 하나 받기
    //=> 1) 중단 문자열이 나왔으면 Stop
    //    2) 앞의 글과 같은 꼬리가 LOOP_MIN_CHARS 이상이면 Loop(그 꼬리 안의 첫 문장 경계)
    //    3) 아니면 붙들 꼬리를 뺀 앞부분을 내보낸다
    //
    // -in: piece = 새 토큰의 바이트 / left = 상한까지 남은 토큰 수 / out = 조각 받는 함수
    //
    // -out: Some(Halt) = 여기서 멈춰야 한다 / None = 계속
    // -out: error = 없음
    //------------------------------------------------------------------
    fn push(&mut self, piece: &[u8], left: usize, out: &mut dyn FnMut(&str)) -> Option<Halt> {
        self.all.extend_from_slice(piece);
        if let Some(pos) = self.stops.iter().filter_map(|s| find_sub(&self.all, s)).min() {
            return Some(Halt::Stop(pos));
        }
        let n = self.all.len();
        let rep = repeated_suffix_len(&self.all);
        let rep_chars = n_chars(&self.all[n - rep..]);
        if rep_chars >= LOOP_MIN_CHARS {
            // 겹친 꼬리가 앞 문장 끝("니다. ")부터 걸칠 수 있다 — 그 안의 첫 문장 경계에서 자른다
            let start = n - rep;
            let cut = sentence_ends(&self.all).into_iter().find(|&b| b >= start && b < n).unwrap_or(start);
            return Some(Halt::Loop(cut.max(self.sent)));
        }
        let mut hold = self.stops.iter().map(|s| suffix_prefix(&self.all, s)).max().unwrap_or(0);
        if rep_chars >= HOLD_MIN_CHARS {
            hold = hold.max(rep);
        }
        if left <= TAIL_WINDOW {
            let last = sentence_ends(&self.all).last().copied().unwrap_or(0);
            hold = hold.max(n - last.max(self.sent));
        }
        let safe = utf8_safe_end(&self.all[..n - hold]);
        if safe > self.sent {
            emit(&self.all[self.sent..safe], out);
            self.sent = safe;
        }
        None
    }

    //------------------------------------------------------------------
    // 마무리 — 멈춘 까닭에 따라 끝을 정하고 남은 꼬리를 내보낸다
    //=> · EOG / 중단 문자열: 그대로(중단 문자열 앞까지)
    //    · 반복 루프: 반복이 시작된 곳 앞까지. 그곳이 문장 중간이면 마지막 완결 문장까지 + 표시
    //    · 상한: 마지막 완결 문장까지 + 표시
    //    이미 내보낸 글자 앞으로는 자르지 못한다(그 경우 내보낸 데까지).
    //    완결 문장이 하나도 없으면 있는 그대로 둔다(빈 답보다 낫다).
    //
    // -in: halt = 멈춘 까닭 / out = 조각 받는 함수
    //
    // -out: GenOut = 남긴 답 + 표시
    // -out: error = 없음
    //------------------------------------------------------------------
    fn finish(mut self, halt: Option<Halt>, out: &mut dyn FnMut(&str)) -> GenOut {
        let mut end = match halt {
            Some(Halt::Stop(p)) | Some(Halt::Loop(p)) => p,
            _ => self.all.len(),
        };
        let mut note = None;
        if matches!(halt, Some(Halt::Loop(_)) | Some(Halt::Capped)) {
            let head = &self.all[..end];
            let last = sentence_ends(head).last().copied().unwrap_or(0);
            let open = last < rstrip_len(head); // 마지막 문장이 덜 끝났다
            if halt == Some(Halt::Capped) {
                note = Some(NOTE_CAPPED);
            } else if open {
                note = Some(NOTE_LOOP);
            }
            if open && last > 0 {
                end = last.max(self.sent);
            }
        }
        if end > self.sent {
            // 남은 꼬리 — 깨진 바이트는 버린다(errors="ignore")
            emit(&self.all[self.sent..end], out);
            self.sent = end;
        }
        GenOut { text: String::from_utf8_lossy(&self.all[..end]).replace('\u{FFFD}', ""), note }
    }
}

// 바이트 조각을 글자로 내보내기(깨진 바이트는 버린다)
fn emit(b: &[u8], out: &mut dyn FnMut(&str)) {
    let s = String::from_utf8_lossy(b).replace('\u{FFFD}', "");
    if !s.is_empty() {
        out(&s);
    }
}

// UTF-8 글자 수(이어지는 바이트 10xxxxxx 를 빼고 센다)
fn n_chars(b: &[u8]) -> usize {
    b.iter().filter(|&&c| (c & 0xC0) != 0x80).count()
}

// 끝 공백을 뺀 길이(바이트)
fn rstrip_len(b: &[u8]) -> usize {
    let mut n = utf8_safe_end(b);
    while n > 0 && b[n - 1].is_ascii_whitespace() {
        n -= 1;
    }
    n
}

//------------------------------------------------------------------
// 앞에 나온 글과 같은 가장 긴 꼬리(바이트)
//=> 뒤집은 바이트의 Z-함수: z[i] = 맨 앞(= 원래의 꼬리)과 i 에서 시작하는 부분이 같은 길이.
//   i ≥ 1 의 최댓값이 "앞쪽 어딘가에 한 번 더 나온" 가장 긴 꼬리다(겹쳐도 된다 — 주기 반복).
//
// -in: b = 지금까지 생성된 바이트
// -out: 길이(바이트) / error = 없음
//------------------------------------------------------------------
fn repeated_suffix_len(b: &[u8]) -> usize {
    let r: Vec<u8> = b.iter().rev().copied().collect();
    let n = r.len();
    let mut z = vec![0usize; n];
    let (mut l, mut rr, mut best) = (0usize, 0usize, 0usize);
    for i in 1..n {
        if i < rr {
            z[i] = (rr - i).min(z[i - l]);
        }
        while i + z[i] < n && r[z[i]] == r[i + z[i]] {
            z[i] += 1;
        }
        if i + z[i] > rr {
            l = i;
            rr = i + z[i];
        }
        best = best.max(z[i]);
    }
    best
}

//------------------------------------------------------------------
// 문장이 끝나는 자리(바이트 위치, 오름차순)
//=> 줄바꿈 뒤, 또는 '.' '!' '?' '。' 뒤에 공백·'['·글 끝이 오는 자리.
//   바로 뒤에 붙은 인용 번호 " [1][2]" 는 그 문장에 넣는다.
//   글 끝의 "6." 처럼 숫자 뒤 마침표는 소수점이 이어질 수 있어 문장 끝으로 보지 않는다.
//
// -in: b = 바이트(끝의 덜 끝난 UTF-8 은 무시)
// -out: 위치 목록 / error = 없음
//------------------------------------------------------------------
fn sentence_ends(b: &[u8]) -> Vec<usize> {
    let s = String::from_utf8_lossy(&b[..utf8_safe_end(b)]);
    // lossy 로 바뀐 글자가 있으면 바이트 위치가 어긋난다 — 그때는 문장 경계를 찾지 않는다
    if s.len() != utf8_safe_end(b) {
        return vec![];
    }
    let cs: Vec<(usize, char)> = s.char_indices().collect();
    let mut out: Vec<usize> = vec![];
    for k in 0..cs.len() {
        let (i, c) = cs[k];
        let next = cs.get(k + 1).map(|x| x.1);
        let end = match c {
            '\n' => true,
            '.' | '!' | '?' | '。' => match next {
                None => !(c == '.' && k > 0 && cs[k - 1].1.is_ascii_digit()),
                Some(nc) => nc.is_whitespace() || nc == '[',
            },
            _ => false,
        };
        if !end {
            continue;
        }
        let mut p = i + c.len_utf8();
        // 문장 끝에 붙은 인용 번호까지 그 문장으로
        if c != '\n' {
            if let Some(m) = CITES_AFTER.find(&s[p..]) {
                p += m.end();
            }
        }
        if out.last().map(|&q| p > q).unwrap_or(true) {
            out.push(p);
        }
    }
    out
}

static CITES_AFTER: once_cell::sync::Lazy<regex::Regex> =
    once_cell::sync::Lazy::new(|| regex::Regex::new(r"^(?:[ \t]*\[\d+\])+").unwrap());

#[cfg(test)]
mod tests {
    use super::*;

    // 조각들을 차례로 넣어 (화면에 나간 글, 결과) 를 돌려준다. left 는 조각마다 상한까지 남은 토큰 수
    fn run(pieces: &[&str], max_new: usize, capped_at_end: bool) -> (String, GenOut) {
        let mut g = OutGuard::new(vec!["<|im_end|>"]);
        let mut shown = String::new();
        let mut halt = None;
        for (k, p) in pieces.iter().enumerate() {
            let left = max_new.saturating_sub(k + 1);
            if let Some(h) = g.push(p.as_bytes(), left, &mut |s| shown.push_str(s)) {
                halt = Some(h);
                break;
            }
        }
        if halt.is_none() && capped_at_end {
            halt = Some(Halt::Capped);
        }
        let o = g.finish(halt, &mut |s| shown.push_str(s));
        (shown, o)
    }

    // 글을 3글자씩 조각낸다(토큰 흉내)
    fn split3(s: &str) -> Vec<String> {
        let cs: Vec<char> = s.chars().collect();
        cs.chunks(3).map(|c| c.iter().collect()).collect()
    }

    #[test]
    fn loop_sentence_is_cut_and_never_shown() {
        // 사용자 보고와 같은 모양 — 같은 문장이 되풀이되다 상한에서 끊긴다
        let s = "[1] 번호의 근거에 따르면, 경조사휴가의 금액은 사망 시 6 일이지만, 배우자의 부모 사망 시 5 일이 부여됩니다. ";
        let text = format!("경조사휴가는 사망 시 주어집니다. {}{}{}배우", s, s, s);
        let ps = split3(&text);
        let refs: Vec<&str> = ps.iter().map(|x| x.as_str()).collect();
        let (shown, o) = run(&refs, 200, false);
        // 문장 끝 바로 뒤의 "[1]" 은 앞 문장의 인용 번호로 본다(다음 문장 머리인지 가릴 수 없다)
        let want = format!("경조사휴가는 사망 시 주어집니다. {} [1]", s.trim_end());
        assert_eq!(o.text.trim_end(), want);
        assert_eq!(shown.trim_end(), want);
        assert_eq!(o.note, None); // 문장 경계에서 잘렸으니 표시는 없다
    }

    #[test]
    fn short_repeats_are_not_loops() {
        // 표 줄 · 같은 머리말 · 숫자 나열처럼 짧은 되풀이는 끊지 않는다
        let text = "| 부장 | 가 | 176 |\n| 차장 | 가 | 176 |\n[1] 근거에 따르면, 숙박비는 실비입니다. [1] 근거에 따르면, 일비는 2만원입니다. 1, 2, 3, 1, 2, 3.";
        let ps = split3(text);
        let refs: Vec<&str> = ps.iter().map(|x| x.as_str()).collect();
        let (shown, o) = run(&refs, 200, false);
        assert_eq!(o.text, text);
        assert_eq!(shown, text);
        assert_eq!(o.note, None);
    }

    #[test]
    fn capped_keeps_last_full_sentence() {
        // 상한에서 멈추면 마지막 완결 문장(뒤 인용 번호 포함)까지 + 표시
        let text = "첫째 문장입니다. [1] 둘째 문장은 휴가가 5일 주어진다는 것입니다. [2][3] 셋째 문장은 아직 끝나지";
        let ps = split3(text);
        let refs: Vec<&str> = ps.iter().map(|x| x.as_str()).collect();
        // 상한을 조각 수로 — 전부 TAIL_WINDOW 안쪽이라 덜 끝난 문장은 화면에 나가지 않는다
        let (shown, o) = run(&refs, refs.len(), true);
        let want = "첫째 문장입니다. [1] 둘째 문장은 휴가가 5일 주어진다는 것입니다. [2][3]";
        assert_eq!(o.text, want);
        assert_eq!(shown, want);
        assert_eq!(o.note, Some(NOTE_CAPPED));
    }

    #[test]
    fn capped_without_full_sentence_keeps_all() {
        let (_, o) = run(&["끝나지 ", "않은 ", "문장"], 3, true);
        assert_eq!(o.text, "끝나지 않은 문장");
        assert_eq!(o.note, Some(NOTE_CAPPED));
    }

    #[test]
    fn stop_string_and_natural_end_unchanged() {
        let (shown, o) = run(&["답은 ", "5일입니다", ".<|im", "_end|>"], 200, false);
        assert_eq!(o.text, "답은 5일입니다.");
        assert_eq!(shown, "답은 5일입니다.");
        assert_eq!(o.note, None);
        let (_, o) = run(&["답은 ", "5일"], 200, false);
        assert_eq!(o.text, "답은 5일");
    }

    #[test]
    fn sentence_end_rules() {
        let s = "값은 3.5일입니다. [1] 다음\n끝은 6.";
        let ends: Vec<&str> = sentence_ends(s.as_bytes()).into_iter().map(|p| &s[..p]).collect();
        assert_eq!(ends, vec!["값은 3.5일입니다. [1]", "값은 3.5일입니다. [1] 다음\n"]);
        assert_eq!(repeated_suffix_len("abcXabc".as_bytes()), 3);
        assert_eq!(repeated_suffix_len("ababab".as_bytes()), 4);
    }
}
