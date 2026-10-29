//------------------------------------------------------------------
// ONNX 임베더 + 리랭커 (Python embed/onnx_embedder.py · retrieve/reranker.py)
//=> onnxruntime.dll(Python 판과 같은 1.29)을 실행 시점에 적재해 쓴다.
//    · 임베더: e5-small-ko — "passage: "/"query: " 접두, 평균 풀링 + L2 정규화, 배치 8, 스레드 4
//    · 리랭커: bge-reranker-base int8 크로스인코더 — (질문, 근거) 쌍마다 점수 하나
//   ensure_loaded() 는 잠금으로 보호돼 예열 스레드와 질문이 겹쳐도 한 번만 올린다.
//------------------------------------------------------------------

use std::sync::Mutex;
use std::time::Instant;

use ort::session::{builder::GraphOptimizationLevel, Session, SessionInputValue};
use ort::value::Tensor;

use crate::config::{self, cfg};
use crate::pyutil;
use crate::tok::Tok;

//------------------------------------------------------------------
// onnxruntime.dll 위치 지정(ORT_DYLIB_PATH)
//=> exe 옆 → exe 옆 _internal\onnxruntime\capi (파이썬 배포본 모양) 순으로 찾는다.
//------------------------------------------------------------------
pub fn ensure_dylib() {
    if std::env::var("ORT_DYLIB_PATH").map(|v| !v.is_empty()).unwrap_or(false) {
        return;
    }
    let d = pyutil::exe_dir();
    for p in [d.join("onnxruntime.dll"), d.join("_internal").join("onnxruntime").join("capi").join("onnxruntime.dll")] {
        if p.is_file() {
            std::env::set_var("ORT_DYLIB_PATH", p);
            return;
        }
    }
}

// 세션 만들기(공통)
fn make_session(path: &str, threads: usize, inter_one: bool) -> Result<Session, String> {
    ensure_dylib();
    // ort 는 DLL 을 못 찾으면 패닉한다 — 오류문으로 바꾼다
    let path = path.to_string();
    std::panic::catch_unwind(move || -> Result<Session, String> {
        let mut b = Session::builder().map_err(|e| e.to_string())?
            .with_optimization_level(GraphOptimizationLevel::Level3).map_err(|e| e.to_string())?
            .with_intra_threads(threads).map_err(|e| e.to_string())?;
        if inter_one {
            b = b.with_inter_threads(1).map_err(|e| e.to_string())?;
        }
        b.commit_from_file(&path).map_err(|e| e.to_string())
    })
    .unwrap_or_else(|_| Err("onnxruntime.dll 을 적재할 수 없습니다".to_string()))
}

//------------------------------------------------------------------
// 임베더
// -필드: tok     = e5 토크나이저(잘라내기 끔) — 청킹·BM25 도 이것을 빌려 쓴다
// -필드: session = ONNX 세션(잠금 — 추론은 한 번에 하나)
// -필드: inputs  = 모델이 받는 입력 이름
//------------------------------------------------------------------
pub struct Embedder {
    state: Mutex<Option<EmbedState>>,
    load_lock: Mutex<()>,
}

struct EmbedState {
    tok: std::sync::Arc<Tok>,
    session: Session,
    inputs: Vec<String>,
}

impl Default for Embedder {
    fn default() -> Self {
        Self::new()
    }
}

impl Embedder {
    pub fn new() -> Embedder {
        Embedder { state: Mutex::new(None), load_lock: Mutex::new(()) }
    }

    pub fn is_ready(&self) -> bool {
        self.state.lock().unwrap().is_some()
    }

    //------------------------------------------------------------------
    // 적재 보장 — 걸린 ms(이미 있으면 0)
    // -out: error = model.onnx·tokenizer.json 없음, 세션 생성 실패면 Err("EmbedError: …")
    //------------------------------------------------------------------
    pub fn ensure_loaded(&self) -> Result<f64, String> {
        if self.is_ready() {
            return Ok(0.0);
        }
        let _g = self.load_lock.lock().unwrap();
        if self.is_ready() {
            return Ok(0.0);
        }
        let t0 = Instant::now();
        let mdir = &cfg().model_dir;
        let onnx = pyutil::join(mdir, "model.onnx");
        let tokp = pyutil::join(mdir, "tokenizer.json");
        if !std::path::Path::new(&onnx).is_file() {
            return Err(format!("EmbedError: model.onnx 없음: {}", onnx));
        }
        if !std::path::Path::new(&tokp).is_file() {
            return Err(format!("EmbedError: tokenizer.json 없음: {}", tokp));
        }
        let tok = Tok::from_file(&tokp).map_err(|e| format!("EmbedError: {}", e))?;
        let session = make_session(&onnx, config::EMBED_THREADS, false)
            .map_err(|e| format!("EmbedError: onnxruntime 세션 생성 실패: {}", e))?;
        let inputs = session.inputs.iter().map(|i| i.name.clone()).collect();
        *self.state.lock().unwrap() = Some(EmbedState { tok: std::sync::Arc::new(tok), session, inputs });
        Ok(t0.elapsed().as_secs_f64() * 1000.0)
    }

    //------------------------------------------------------------------
    // 토크나이저 (적재 후)
    //------------------------------------------------------------------
    pub fn tokenizer(&self) -> Result<std::sync::Arc<Tok>, String> {
        self.ensure_loaded()?;
        Ok(self.state.lock().unwrap().as_ref().unwrap().tok.clone())
    }

    //------------------------------------------------------------------
    // 글 묶음 → 정규화 벡터들
    //=> 특수 토큰을 붙여 토큰화한 뒤 앞 512개만 쓴다(Python 과 같이 끝의 </s> 도 잘린다).
    //   배치 안 가장 긴 길이로 0 을 채우고, attention mask 로 평균 풀링한다.
    //------------------------------------------------------------------
    pub fn embed(&self, texts: &[String], prefix: &str) -> Result<Vec<Vec<f32>>, String> {
        self.ensure_loaded()?;
        if texts.is_empty() {
            return Ok(vec![]);
        }
        let mut st = self.state.lock().unwrap();
        let st = st.as_mut().unwrap();
        let cap = config::EMBED_MAX_TOKENS;
        let mut encs: Vec<(Vec<i64>, Vec<i64>)> = vec![];
        for t in texts {
            let e = st.tok.inner.encode(format!("{}{}", prefix, t), true).map_err(|e| format!("EmbedError: {}", e))?;
            let n = e.get_ids().len().min(cap);
            encs.push((e.get_ids()[..n].iter().map(|&x| x as i64).collect(),
                       e.get_attention_mask()[..n].iter().map(|&x| x as i64).collect()));
        }
        let width = encs.iter().map(|e| e.0.len()).max().unwrap_or(1).max(1);
        let b = encs.len();
        let mut ids = vec![0i64; b * width];
        let mut attn = vec![0i64; b * width];
        for (i, (eid, em)) in encs.iter().enumerate() {
            ids[i * width..i * width + eid.len()].copy_from_slice(eid);
            attn[i * width..i * width + em.len()].copy_from_slice(em);
        }
        let mut feeds: Vec<(String, SessionInputValue)> = vec![];
        let shape = [b, width];
        if st.inputs.iter().any(|n| n == "input_ids") {
            feeds.push(("input_ids".into(), Tensor::from_array((shape, ids.clone())).map_err(|e| e.to_string())?.into()));
        }
        if st.inputs.iter().any(|n| n == "attention_mask") {
            feeds.push(("attention_mask".into(), Tensor::from_array((shape, attn.clone())).map_err(|e| e.to_string())?.into()));
        }
        if st.inputs.iter().any(|n| n == "token_type_ids") {
            feeds.push(("token_type_ids".into(), Tensor::from_array((shape, vec![0i64; b * width])).map_err(|e| e.to_string())?.into()));
        }
        let outputs = st.session.run(feeds).map_err(|e| format!("EmbedError: 추론 실패: {}", e))?;
        let (oshape, data) = outputs[0].try_extract_tensor::<f32>().map_err(|e| format!("EmbedError: {}", e))?;
        let t = oshape[1] as usize;
        let dim = oshape[2] as usize;
        let mut out = vec![];
        for i in 0..b {
            let mut v = vec![0f32; dim];
            let mut cnt = 0f32;
            for j in 0..t.min(width) {
                let m = attn[i * width + j] as f32;
                if m == 0.0 {
                    continue;
                }
                cnt += m;
                let base = (i * t + j) * dim;
                for k in 0..dim {
                    v[k] += data[base + k] * m;
                }
            }
            let cnt = cnt.max(1e-9);
            for x in v.iter_mut() {
                *x /= cnt;
            }
            let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt().max(1e-9);
            for x in v.iter_mut() {
                *x /= norm;
            }
            out.push(v);
        }
        Ok(out)
    }

    //------------------------------------------------------------------
    // 청크들 임베딩(배치 8) — on_batch(끝낸 수, 전체)
    //------------------------------------------------------------------
    pub fn embed_passages(&self, chunks: &[String]) -> Result<Vec<Vec<f32>>, String> {
        let mut out = Vec::with_capacity(chunks.len());
        for part in chunks.chunks(config::EMBED_BATCH) {
            out.extend(self.embed(part, config::PASSAGE_PREFIX)?);
        }
        Ok(out)
    }

    // 질문 하나 임베딩
    pub fn embed_query(&self, q: &str) -> Result<Vec<f32>, String> {
        Ok(self.embed(&[q.to_string()], config::QUERY_PREFIX)?.remove(0))
    }
}

//------------------------------------------------------------------
// 리랭커 (크로스인코더)
//------------------------------------------------------------------
pub struct Reranker {
    state: Mutex<Option<(tokenizers::Tokenizer, Session)>>,
    load_lock: Mutex<()>,
}

impl Default for Reranker {
    fn default() -> Self {
        Self::new()
    }
}

impl Reranker {
    pub fn new() -> Reranker {
        Reranker { state: Mutex::new(None), load_lock: Mutex::new(()) }
    }

    pub fn is_ready(&self) -> bool {
        self.state.lock().unwrap().is_some()
    }

    //------------------------------------------------------------------
    // 적재 보장 — 걸린 ms
    //=> 잘라내기 512(LongestFirst), 채우기는 배치 최장 길이·pad id 1(XLM-R).
    // -out: error = 모델 파일 없음은 Err("RerankError: 리랭커 모델 없음: …")
    //------------------------------------------------------------------
    pub fn ensure_loaded(&self) -> Result<f64, String> {
        if self.is_ready() {
            return Ok(0.0);
        }
        let _g = self.load_lock.lock().unwrap();
        if self.is_ready() {
            return Ok(0.0);
        }
        let dir = &cfg().rerank_dir;
        let onnx = pyutil::join(dir, "model_int8.onnx");
        let tokp = pyutil::join(dir, "tokenizer.json");
        for p in [&onnx, &tokp] {
            if !std::path::Path::new(p).is_file() {
                return Err(format!("RerankError: 리랭커 모델 없음: {}", p));
            }
        }
        let t0 = Instant::now();
        let session = make_session(&onnx, config::EMBED_THREADS, true)?;
        let mut tok = tokenizers::Tokenizer::from_file(&tokp).map_err(|e| e.to_string())?;
        tok.with_truncation(Some(tokenizers::TruncationParams {
            max_length: config::RERANK_MAX_TOKENS,
            ..Default::default()
        })).map_err(|e| e.to_string())?;
        tok.with_padding(Some(tokenizers::PaddingParams {
            strategy: tokenizers::PaddingStrategy::BatchLongest,
            pad_id: 1,
            pad_token: "<pad>".into(),
            ..Default::default()
        }));
        *self.state.lock().unwrap() = Some((tok, session));
        Ok(t0.elapsed().as_secs_f64() * 1000.0)
    }

    //------------------------------------------------------------------
    // (질문, 근거) 쌍 점수 — 높을수록 관련
    //------------------------------------------------------------------
    pub fn score(&self, query: &str, passages: &[String]) -> Result<Vec<f32>, String> {
        self.ensure_loaded()?;
        if passages.is_empty() {
            return Ok(vec![]);
        }
        let mut st = self.state.lock().unwrap();
        let (tok, sess) = st.as_mut().unwrap();
        let pairs: Vec<tokenizers::EncodeInput> = passages.iter()
            .map(|p| tokenizers::EncodeInput::Dual(query.to_string().into(), p.clone().into())).collect();
        let encs = tok.encode_batch(pairs, true).map_err(|e| e.to_string())?;
        let b = encs.len();
        let w = encs.iter().map(|e| e.get_ids().len()).max().unwrap_or(1);
        let mut ids = vec![1i64; b * w];
        let mut mask = vec![0i64; b * w];
        for (i, e) in encs.iter().enumerate() {
            for (j, (&id, &m)) in e.get_ids().iter().zip(e.get_attention_mask()).enumerate() {
                ids[i * w + j] = id as i64;
                mask[i * w + j] = m as i64;
            }
        }
        let feeds: Vec<(String, SessionInputValue)> = vec![
            ("input_ids".into(), Tensor::from_array(([b, w], ids)).map_err(|e| e.to_string())?.into()),
            ("attention_mask".into(), Tensor::from_array(([b, w], mask)).map_err(|e| e.to_string())?.into()),
        ];
        let out = sess.run(feeds).map_err(|e| e.to_string())?;
        let (_, data) = out[0].try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
        Ok(data.to_vec())
    }

    //------------------------------------------------------------------
    // 점수 — 길이순으로 batch 개씩 나눠서 (탐색기 모드처럼 후보가 많을 때)
    //=> score 는 한 묶음을 가장 긴 글에 맞춰 채우므로, 후보가 많고 길이가 들쭉날쭉하면 채운 칸 계산이 커진다.
    //   비슷한 길이끼리 작게 묶으면 같은 결과를 더 빨리 낸다. 돌려주는 순서는 passages 순서 그대로.
    //------------------------------------------------------------------
    pub fn score_batched(&self, query: &str, passages: &[String], batch: usize) -> Result<Vec<f32>, String> {
        let mut order: Vec<usize> = (0..passages.len()).collect();
        order.sort_by_key(|&i| passages[i].chars().count());
        let mut out = vec![0f32; passages.len()];
        for part in order.chunks(batch.max(1)) {
            let texts: Vec<String> = part.iter().map(|&i| passages[i].clone()).collect();
            let s = self.score(query, &texts)?;
            for (k, &i) in part.iter().enumerate() {
                out[i] = s[k];
            }
        }
        Ok(out)
    }
}
