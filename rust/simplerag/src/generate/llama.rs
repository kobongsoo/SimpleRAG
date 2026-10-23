//------------------------------------------------------------------
// llama.dll 직접 호출 (llama-cpp-python 0.3.35 의 DLL 을 그대로 쓴다)
//=> Vulkan SDK 없이 iGPU 가속을 쓰려고, 파이썬 배포본이 동봉한 llama.dll(CPU 판·Vulkan 판)을
//   libloading 으로 적재해 필요한 C 함수만 부른다.
//    · 구조체(llama_model_params · llama_context_params)는 DLL 의 기본값 함수로 받아 와
//      필요한 칸만 고친다 — Win64 에서 큰 구조체는 포인터로 오가므로 뒤에 여유 공간을 둬도 안전하다
//    · 칸 위치(오프셋)는 llama-cpp-python 0.3.35 ctypes 정의에서 잰 값이다
//    · 한 프로세스에서 DLL 은 한 번만 고를 수 있다(Python 판과 같은 제약)
//------------------------------------------------------------------

use std::ffi::{c_char, c_void, CString};
use std::sync::OnceLock;

use libloading::Library;

pub type Token = i32;

// 기본값 구조체를 받을 넉넉한 버퍼(실제 크기: 모델 72, 컨텍스트 160 바이트)
#[repr(C, align(8))]
#[derive(Clone, Copy)]
pub struct ModelParams(pub [u8; 512]);
#[repr(C, align(8))]
#[derive(Clone, Copy)]
pub struct ContextParams(pub [u8; 1024]);

// llama_batch (56 바이트)
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Batch {
    pub n_tokens: i32,
    pub token: *mut Token,
    pub embd: *mut f32,
    pub pos: *mut i32,
    pub n_seq_id: *mut i32,
    pub seq_id: *mut *mut i32,
    pub logits: *mut i8,
}

// 구조체 칸에 값 쓰기
fn put<T: Copy>(buf: &mut [u8], off: usize, v: T) {
    unsafe { std::ptr::write_unaligned(buf.as_mut_ptr().add(off) as *mut T, v) }
}

//------------------------------------------------------------------
// 쓰는 함수 묶음
//------------------------------------------------------------------
pub struct Api {
    _libs: Vec<Library>,
    pub dir: String,
    backend_init: unsafe extern "C" fn(),
    log_set: Option<unsafe extern "C" fn(*const c_void, *mut c_void)>,
    model_default_params: unsafe extern "C" fn() -> ModelParams,
    context_default_params: unsafe extern "C" fn() -> ContextParams,
    model_load: unsafe extern "C" fn(*const c_char, ModelParams) -> *mut c_void,
    model_free: unsafe extern "C" fn(*mut c_void),
    init_from_model: unsafe extern "C" fn(*mut c_void, ContextParams) -> *mut c_void,
    free_ctx: unsafe extern "C" fn(*mut c_void),
    supports_gpu: unsafe extern "C" fn() -> bool,
    model_get_vocab: unsafe extern "C" fn(*const c_void) -> *const c_void,
    tokenize: unsafe extern "C" fn(*const c_void, *const c_char, i32, *mut Token, i32, bool, bool) -> i32,
    token_to_piece: unsafe extern "C" fn(*const c_void, Token, *mut c_char, i32, i32, bool) -> i32,
    batch_get_one: unsafe extern "C" fn(*mut Token, i32) -> Batch,
    decode: unsafe extern "C" fn(*mut c_void, Batch) -> i32,
    get_logits_ith: unsafe extern "C" fn(*mut c_void, i32) -> *mut f32,
    get_memory: unsafe extern "C" fn(*mut c_void) -> *mut c_void,
    memory_seq_rm: unsafe extern "C" fn(*mut c_void, i32, i32, i32) -> bool,
    vocab_n_tokens: unsafe extern "C" fn(*const c_void) -> i32,
    vocab_is_eog: unsafe extern "C" fn(*const c_void, Token) -> bool,
    vocab_bos: unsafe extern "C" fn(*const c_void) -> Token,
    vocab_eos: unsafe extern "C" fn(*const c_void) -> Token,
    vocab_get_add_bos: unsafe extern "C" fn(*const c_void) -> bool,
    vocab_get_add_eos: unsafe extern "C" fn(*const c_void) -> bool,
    vocab_sep: Option<unsafe extern "C" fn(*const c_void) -> Token>,
}

unsafe impl Send for Api {}
unsafe impl Sync for Api {}

static API: OnceLock<Result<Api, String>> = OnceLock::new();

// llama.cpp 로그를 버리는 콜백(verbose=False 와 같은 효과)
unsafe extern "C" fn quiet_log(_level: i32, _text: *const c_char, _ud: *mut c_void) {}

//------------------------------------------------------------------
// DLL 적재 — 폴더 하나(그 폴더의 ggml*.dll 도 함께 찾게 한다)
//------------------------------------------------------------------
fn load(dir: &str) -> Result<Api, String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::System::LibraryLoader::{LoadLibraryExW, LOAD_WITH_ALTERED_SEARCH_PATH};
    let path = std::path::Path::new(dir).join("llama.dll");
    if !path.is_file() {
        return Err(format!("llama.dll 없음: {}", path.display()));
    }
    unsafe {
        // 같은 폴더의 ggml-*.dll 을 먼저 그 폴더에서 찾도록 '바뀐 검색 경로'로 적재한다
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let h = LoadLibraryExW(wide.as_ptr(), std::ptr::null_mut(), LOAD_WITH_ALTERED_SEARCH_PATH);
        if h.is_null() {
            return Err(format!("llama.dll 적재 실패: {} (오류 {})", path.display(), std::io::Error::last_os_error()));
        }
        let lib: Library = libloading::os::windows::Library::from_raw(h as _).into();
        macro_rules! sym {
            ($n:expr) => {{
                let s: libloading::Symbol<*const c_void> = lib.get($n).map_err(|e| format!("llama 함수 없음 {}: {}", String::from_utf8_lossy($n), e))?;
                std::mem::transmute::<*const c_void, _>(*s)
            }};
        }
        macro_rules! opt {
            ($n:expr) => {{
                match lib.get::<*const c_void>($n) {
                    Ok(s) => Some(std::mem::transmute::<*const c_void, _>(*s)),
                    Err(_) => None,
                }
            }};
        }
        let api = Api {
            dir: dir.to_string(),
            backend_init: sym!(b"llama_backend_init\0"),
            log_set: opt!(b"llama_log_set\0"),
            model_default_params: sym!(b"llama_model_default_params\0"),
            context_default_params: sym!(b"llama_context_default_params\0"),
            model_load: sym!(b"llama_model_load_from_file\0"),
            model_free: sym!(b"llama_model_free\0"),
            init_from_model: sym!(b"llama_init_from_model\0"),
            free_ctx: sym!(b"llama_free\0"),
            supports_gpu: sym!(b"llama_supports_gpu_offload\0"),
            model_get_vocab: sym!(b"llama_model_get_vocab\0"),
            tokenize: sym!(b"llama_tokenize\0"),
            token_to_piece: sym!(b"llama_token_to_piece\0"),
            batch_get_one: sym!(b"llama_batch_get_one\0"),
            decode: sym!(b"llama_decode\0"),
            get_logits_ith: sym!(b"llama_get_logits_ith\0"),
            get_memory: sym!(b"llama_get_memory\0"),
            memory_seq_rm: sym!(b"llama_memory_seq_rm\0"),
            vocab_n_tokens: sym!(b"llama_vocab_n_tokens\0"),
            vocab_is_eog: sym!(b"llama_vocab_is_eog\0"),
            vocab_bos: sym!(b"llama_vocab_bos\0"),
            vocab_eos: sym!(b"llama_vocab_eos\0"),
            vocab_get_add_bos: sym!(b"llama_vocab_get_add_bos\0"),
            vocab_get_add_eos: sym!(b"llama_vocab_get_add_eos\0"),
            vocab_sep: opt!(b"llama_vocab_sep\0"),
            _libs: vec![lib],
        };
        if let Some(ls) = api.log_set {
            ls(quiet_log as *const c_void, std::ptr::null_mut());
        }
        (api.backend_init)();
        Ok(api)
    }
}

//------------------------------------------------------------------
// 이 프로세스의 llama.dll 고르기(한 번만) — 이미 골랐으면 그 DLL
//------------------------------------------------------------------
pub fn api_for(dir: &str) -> Result<&'static Api, String> {
    match API.get_or_init(|| load(dir)) {
        Ok(a) => Ok(a),
        Err(e) => Err(e.clone()),
    }
}

// 이미 적재된 DLL(없으면 None)
pub fn loaded() -> Option<&'static Api> {
    API.get().and_then(|r| r.as_ref().ok())
}

impl Api {
    pub fn supports_gpu_offload(&self) -> bool {
        unsafe { (self.supports_gpu)() }
    }
}

//------------------------------------------------------------------
// 적재한 모델 + 컨텍스트 하나
// -필드: cache = 지금 KV 에 들어 있는 토큰(앞부분이 같으면 다시 계산하지 않는다)
//------------------------------------------------------------------
pub struct Model {
    api: &'static Api,
    model: *mut c_void,
    ctx: *mut c_void,
    vocab: *const c_void,
    n_vocab: i32,
    pub n_ctx: u32,
    n_batch: u32,
    cache: Vec<Token>,
}

unsafe impl Send for Model {}

impl Drop for Model {
    fn drop(&mut self) {
        unsafe {
            (self.api.free_ctx)(self.ctx);
            (self.api.model_free)(self.model);
        }
    }
}

//------------------------------------------------------------------
// 적재 인자
//------------------------------------------------------------------
pub struct LoadOpts {
    pub n_ctx: u32,
    pub n_batch: u32,
    pub n_threads: i32,
    pub n_gpu_layers: i32,
    pub flash_attn: bool,
}

impl Model {
    //------------------------------------------------------------------
    // 모델 적재 — llama-cpp-python Llama(...) 가 넣는 값과 같게 칸을 채운다
    //------------------------------------------------------------------
    pub fn load(api: &'static Api, path: &str, o: &LoadOpts) -> Result<Model, String> {
        unsafe {
            let mut mp = (api.model_default_params)();
            // n_gpu_layers(16) — -1 이면 INT32 최대(전 레이어)
            put::<i32>(&mut mp.0, 16, if o.n_gpu_layers == -1 { 0x7FFF_FFFF } else { o.n_gpu_layers });
            put::<i32>(&mut mp.0, 20, 1); // split_mode = LAYER
            put::<i32>(&mut mp.0, 24, 1); // load_mode = MMAP
            put::<i32>(&mut mp.0, 28, 0); // main_gpu
            put::<u8>(&mut mp.0, 64, 0); // vocab_only
            let cpath = CString::new(path.as_bytes()).map_err(|e| e.to_string())?;
            let model = (api.model_load)(cpath.as_ptr(), mp);
            if model.is_null() {
                return Err(format!("Failed to load model from file: {}", path));
            }
            let mut cp = (api.context_default_params)();
            put::<u32>(&mut cp.0, 0, o.n_ctx);
            put::<u32>(&mut cp.0, 4, o.n_batch);
            put::<u32>(&mut cp.0, 8, o.n_batch.min(512)); // n_ubatch
            put::<i32>(&mut cp.0, 28, o.n_threads);
            put::<i32>(&mut cp.0, 32, o.n_threads);
            put::<i32>(&mut cp.0, 40, -1); // rope_scaling_type = UNSPECIFIED
            put::<i32>(&mut cp.0, 44, -1); // pooling_type
            put::<i32>(&mut cp.0, 48, -1); // attention_type
            put::<i32>(&mut cp.0, 52, if o.flash_attn { 1 } else { 0 });
            put::<f32>(&mut cp.0, 56, 0.0); // rope_freq_base
            put::<f32>(&mut cp.0, 60, 0.0); // rope_freq_scale
            put::<f32>(&mut cp.0, 64, -1.0); // yarn_ext_factor
            put::<f32>(&mut cp.0, 68, 1.0); // yarn_attn_factor
            put::<f32>(&mut cp.0, 72, 32.0); // yarn_beta_fast
            put::<f32>(&mut cp.0, 76, 1.0); // yarn_beta_slow
            put::<u32>(&mut cp.0, 80, 0); // yarn_orig_ctx
            put::<u8>(&mut cp.0, 128, 0); // embeddings
            put::<u8>(&mut cp.0, 129, 1); // offload_kqv
            put::<u8>(&mut cp.0, 130, 0); // no_perf
            let ctx = (api.init_from_model)(model, cp);
            if ctx.is_null() {
                (api.model_free)(model);
                return Err("Failed to create llama_context".into());
            }
            let vocab = (api.model_get_vocab)(model);
            let n_vocab = (api.vocab_n_tokens)(vocab);
            Ok(Model { api, model, ctx, vocab, n_vocab, n_ctx: o.n_ctx, n_batch: o.n_batch, cache: vec![] })
        }
    }

    //------------------------------------------------------------------
    // 글 → 토큰 (add_bos=False, special=True 가 기본 쓰임)
    //------------------------------------------------------------------
    pub fn tokenize(&self, text: &str, add_bos: bool, special: bool) -> Vec<Token> {
        let b = text.as_bytes();
        let mut cap = (b.len() + 16) as i32;
        loop {
            let mut out = vec![0 as Token; cap as usize];
            let n = unsafe { (self.api.tokenize)(self.vocab, b.as_ptr() as *const c_char, b.len() as i32, out.as_mut_ptr(), cap, add_bos, special) };
            if n >= 0 {
                out.truncate(n as usize);
                return out;
            }
            cap = -n;
        }
    }

    //------------------------------------------------------------------
    // 토큰 하나 → 바이트(special=False 면 특수 토큰은 빈 조각)
    //------------------------------------------------------------------
    pub fn piece(&self, t: Token) -> Vec<u8> {
        let mut buf = vec![0u8; 64];
        let n = unsafe { (self.api.token_to_piece)(self.vocab, t, buf.as_mut_ptr() as *mut c_char, buf.len() as i32, 0, false) };
        if n < 0 {
            buf = vec![0u8; (-n) as usize];
            let n2 = unsafe { (self.api.token_to_piece)(self.vocab, t, buf.as_mut_ptr() as *mut c_char, buf.len() as i32, 0, false) };
            buf.truncate(n2.max(0) as usize);
            return buf;
        }
        buf.truncate(n as usize);
        buf
    }

    pub fn is_eog(&self, t: Token) -> bool {
        unsafe { (self.api.vocab_is_eog)(self.vocab, t) }
    }

    //------------------------------------------------------------------
    // 프롬프트 토큰(create_completion 과 같은 규칙) — [BOS] + 본문(special) + [EOS/SEP]
    //------------------------------------------------------------------
    pub fn prompt_tokens(&self, prompt: &str) -> Vec<Token> {
        unsafe {
            let bos = (self.api.vocab_bos)(self.vocab);
            let eos = (self.api.vocab_eos)(self.vocab);
            let sep = self.api.vocab_sep.map(|f| f(self.vocab)).unwrap_or(-1);
            let mut out = vec![];
            if (self.api.vocab_get_add_bos)(self.vocab) && bos != -1 {
                out.push(bos);
            }
            if !prompt.is_empty() {
                out.extend(self.tokenize(prompt, false, true));
            }
            if (self.api.vocab_get_add_eos)(self.vocab) || sep != -1 {
                out.push(if sep != -1 { sep } else { eos });
            }
            out
        }
    }

    //------------------------------------------------------------------
    // 토큰을 KV 에 넣고 마지막 토큰의 logits 를 얻는다
    //=> 이전 호출과 앞부분이 같은 만큼은 다시 계산하지 않는다(시스템 지시문 KV 재사용).
    //   전부 같으면 마지막 하나는 다시 넣어 logits 를 얻는다.
    //------------------------------------------------------------------
    pub fn eval_prompt(&mut self, tokens: &[Token]) -> Result<Vec<f32>, String> {
        if tokens.is_empty() {
            return Err("빈 프롬프트".into());
        }
        if tokens.len() as u32 > self.n_ctx {
            return Err(format!("Requested tokens ({}) exceed context window of {}", tokens.len(), self.n_ctx));
        }
        let mut keep = self.cache.iter().zip(tokens).take_while(|(a, b)| a == b).count();
        if keep == tokens.len() {
            keep -= 1;
        }
        unsafe {
            let mem = (self.api.get_memory)(self.ctx);
            if !(self.api.memory_seq_rm)(mem, -1, keep as i32, -1) {
                (self.api.memory_seq_rm)(mem, -1, 0, -1);
                keep = 0;
            }
        }
        self.cache.truncate(keep);
        self.feed(&tokens[keep..])
    }

    //------------------------------------------------------------------
    // 토큰 몇 개 이어서 넣기(n_batch 씩) — 마지막 logits
    //------------------------------------------------------------------
    pub fn feed(&mut self, toks: &[Token]) -> Result<Vec<f32>, String> {
        for part in toks.chunks(self.n_batch as usize) {
            let mut v = part.to_vec();
            let rc = unsafe {
                let b = (self.api.batch_get_one)(v.as_mut_ptr(), v.len() as i32);
                (self.api.decode)(self.ctx, b)
            };
            if rc != 0 {
                return Err(format!("llama_decode returned {}", rc));
            }
            self.cache.extend_from_slice(part);
        }
        unsafe {
            let p = (self.api.get_logits_ith)(self.ctx, -1);
            if p.is_null() {
                return Err("logits 없음".into());
            }
            Ok(std::slice::from_raw_parts(p, self.n_vocab as usize).to_vec())
        }
    }

    // KV 비우기(측정용 reset)
    pub fn reset(&mut self) {
        unsafe {
            let mem = (self.api.get_memory)(self.ctx);
            (self.api.memory_seq_rm)(mem, -1, 0, -1);
        }
        self.cache.clear();
    }
}

//------------------------------------------------------------------
// 가장 큰 logit 의 토큰(온도 0 — 탐욕 선택, 같으면 앞 번호)
//------------------------------------------------------------------
pub fn argmax(logits: &[f32]) -> Token {
    let mut best = 0usize;
    for (i, &v) in logits.iter().enumerate() {
        if v > logits[best] {
            best = i;
        }
    }
    best as Token
}
