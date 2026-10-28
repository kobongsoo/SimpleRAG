//------------------------------------------------------------------
// 구성요소 조립 + 병렬 예열 + 2단계 응답 (Python warmup.py · pipeline.py)
//=> 임베더·저장소·BM25·생성 모델을 각각 스레드로 동시에 올려 가장 느린 하나로 수렴시킨다.
//   리랭커는 준비 완료 뒤 백그라운드로 올린다(config.rerank_defer_load).
//   응답은 근거를 먼저 내보내고(evidence), 답을 토큰 단위로 흘린 뒤(token), 요약(done)을 낸다.
//------------------------------------------------------------------

use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::bm25::Bm25Index;
use crate::config::cfg;
use crate::embed::{Embedder, Reranker};
use crate::generate::backend;
use crate::generate::generator::Generator;
use crate::generate::prompts::{parse_answer_opt, recite};
use crate::pyutil;
use crate::retrieve::{self, Chunk, Retriever, Timing};
use crate::store::VectorStore;

// 본문의 인용 번호 — 바로잡은 뒤 인용 목록을 다시 센다
static CITE_NUM: once_cell::sync::Lazy<regex::Regex> =
    once_cell::sync::Lazy::new(|| regex::Regex::new(r"\[(\d+)\]").unwrap());

pub const NO_DOCS_IN_FOLDER: &str = "이 폴더(하위 포함)에는 인덱싱된 문서가 없습니다. 범위를 '지정 폴더 전체' 로 넓히거나, 문서가 인덱싱될 때까지 기다려 주세요.";
// 멀티턴 — 모델에 주는 질문 꼴. 앞 질문은 맥락으로만 쓰게 한다(Python pipeline.TURN_FORMAT 과 같은 글)
pub const TURN_FORMAT: &str = "이전 질문(맥락 참고용 — 이것에는 답하지 마세요): {prev}\n지금 질문: {q}";

//------------------------------------------------------------------
// 멀티턴 질문 나누기 (Python pipeline.turn_queries 와 같다)
//=> 설정이 꺼졌거나 앞 질문이 없으면 그대로. 아니면 검색어 = 앞 질문 + 지금 질문,
//   모델 질문 = "이전 질문(참고용) / 지금 질문". 돌려줌: (검색어, 모델 질문, 이어 붙였나)
//------------------------------------------------------------------
pub fn turn_queries(query: &str, prev: Option<&str>) -> (String, String, bool) {
    let prev = prev.map(|p| p.trim()).unwrap_or("");
    if !cfg().multiturn || prev.is_empty() {
        return (query.to_string(), query.to_string(), false);
    }
    (format!("{} {}", prev, query), TURN_FORMAT.replace("{prev}", prev).replace("{q}", query), true)
}

// 올린 문서가 있던 대화인데 업로드 폴더가 사라졌을 때 — 인덱싱 폴더로 넘어가지 않는다(Python UPLOAD_MISSING 과 같다)
pub const UPLOAD_MISSING: &str = "올린 문서를 찾지 못했습니다 — 문서를 다시 올려 주세요.";

pub const NO_RELEVANT: &str = "질문에 대한 내용이 문서에 없습니다. 답변할 수 없습니다.";

//------------------------------------------------------------------
// 예열 결과 — 구성요소별 ms 또는 오류문
//------------------------------------------------------------------
#[derive(Debug, Default, Clone)]
pub struct Warm {
    pub items: Map<String, Value>,
    pub backend: String,
    pub rerank: bool,
    pub rerank_deferred: bool,
    pub total_ms: f64,
}

//------------------------------------------------------------------
// 앱 — 구성요소 묶음
//------------------------------------------------------------------
pub struct App {
    pub embedder: Arc<Embedder>,
    pub store: Arc<VectorStore>,
    pub bm25: Arc<Bm25Index>,
    pub generator: Arc<Generator>,
    pub reranker: Arc<Reranker>,
    pub retriever: Arc<Retriever>,
    pub backend: Mutex<Option<Value>>,
    // 백그라운드 리랭커 적재 결과: None(안 함·진행 중) | Ok(ms) | Err(사유)
    pub rerank_load: Arc<Mutex<Option<Result<f64, String>>>>,
    rerank_thread: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl App {
    pub fn new(model_alias: Option<&str>) -> App {
        let embedder = Arc::new(Embedder::new());
        let store = Arc::new(VectorStore::new(None));
        let bm25 = Arc::new(Bm25Index::new(None));
        let retriever = Arc::new(Retriever { embedder: embedder.clone(), store: store.clone(), bm25: bm25.clone(), reranker: Mutex::new(None) });
        App {
            embedder,
            store,
            bm25,
            generator: Arc::new(Generator::new(model_alias)),
            reranker: Arc::new(Reranker::new()),
            retriever,
            backend: Mutex::new(None),
            rerank_load: Arc::new(Mutex::new(None)),
            rerank_thread: Mutex::new(None),
        }
    }

    //------------------------------------------------------------------
    // 병렬 예열 (핵심)
    // -in: skip_llm = 생성 모델을 안 올림(인덱싱·검색 전용) / rerank = None 이면 설정·백엔드를 따른다
    //------------------------------------------------------------------
    pub fn warmup(&self, skip_llm: bool, log: &mut dyn FnMut(&str), rerank: Option<bool>) -> Warm {
        let backend_name = if skip_llm {
            backend::peek(Some(&self.generator.alias)).and_then(|c| c["backend"].as_str().map(|s| s.to_string())).unwrap_or_else(|| "cpu".into())
        } else {
            let info = backend::select(Some(&self.generator.alias), false, log);
            let b = info["backend"].as_str().unwrap_or("cpu").to_string();
            *self.backend.lock().unwrap() = Some(info);
            *self.generator.backend.lock().unwrap() = b.clone();
            b
        };
        let use_rerank = rerank.unwrap_or_else(|| backend::rerank_enabled(&backend_name));
        *self.retriever.reranker.lock().unwrap() = if use_rerank { Some(self.reranker.clone()) } else { None };
        let defer = use_rerank && cfg().rerank_defer_load;
        let results: Arc<Mutex<Map<String, Value>>> = Arc::new(Mutex::new(Map::new()));
        let t0 = Instant::now();
        let mut handles = vec![];
        // 구성요소 하나를 스레드로 올리고 결과(ms 또는 오류)를 모은다
        let spawn = |name: &'static str, f: Box<dyn FnOnce() -> Result<f64, String> + Send>, results: Arc<Mutex<Map<String, Value>>>| {
            std::thread::spawn(move || {
                let v = match f() {
                    Ok(ms) => pyutil::jf(ms),
                    Err(e) => json!(err_short(&e)),
                };
                results.lock().unwrap().insert(name.into(), v);
            })
        };
        let e = self.embedder.clone();
        handles.push(spawn("embedder", Box::new(move || e.ensure_loaded()), results.clone()));
        let s = self.store.clone();
        handles.push(spawn("qdrant", Box::new(move || s.ensure_loaded()), results.clone()));
        let b = self.bm25.clone();
        handles.push(spawn("bm25", Box::new(move || Ok(b.ensure_loaded())), results.clone()));
        if use_rerank && !defer {
            let r = self.reranker.clone();
            handles.push(spawn("reranker", Box::new(move || r.ensure_loaded()), results.clone()));
        }
        if !skip_llm {
            let g = self.generator.clone();
            handles.push(spawn("llm", Box::new(move || {
                let ms = g.ensure_loaded()?;
                g.warm()?;
                Ok(ms)
            }), results.clone()));
        }
        for h in handles {
            let _ = h.join();
        }
        if defer {
            self.start_rerank_load();
        }
        let mut items = results.lock().unwrap().clone();
        if items.get("reranker").map(|v| v.is_string()).unwrap_or(false) {
            *self.retriever.reranker.lock().unwrap() = None;
        }
        if let Some(w) = *self.generator.last_warm_ms.lock().unwrap() {
            items.insert("llm_warm".into(), pyutil::jf(w));
        }
        Warm {
            items,
            backend: if skip_llm { backend_name } else { self.generator.backend() },
            rerank: self.retriever.reranker.lock().unwrap().is_some(),
            rerank_deferred: defer,
            total_ms: pyutil::round_to(t0.elapsed().as_secs_f64() * 1000.0, 1),
        }
    }

    // 리랭커 백그라운드 적재 시작
    fn start_rerank_load(&self) {
        let r = self.reranker.clone();
        let slot = self.rerank_load.clone();
        let ret = self.retriever.clone();
        let h = std::thread::spawn(move || {
            let res = r.ensure_loaded();
            if res.is_err() {
                *ret.reranker.lock().unwrap() = None;
            }
            *slot.lock().unwrap() = Some(res.map_err(|e| err_short(&e)));
        });
        *self.rerank_thread.lock().unwrap() = Some(h);
    }

    //------------------------------------------------------------------
    // 백그라운드 리랭커 적재 기다리기 — Some(Ok(ms)) | Some(Err(사유)) | None
    //------------------------------------------------------------------
    pub fn wait_rerank(&self) -> Option<Result<f64, String>> {
        if let Some(h) = self.rerank_thread.lock().unwrap().take() {
            let _ = h.join();
        }
        self.rerank_load.lock().unwrap().clone()
    }

    pub fn close(&self) {
        self.store.close();
    }

    //------------------------------------------------------------------
    // 질의 응답 — 사건을 차례로 on_event 에 넘긴다
    //   prev = 같은 대화의 바로 앞 질문(멀티턴 — turn_queries). None 이면 이 질문만으로
    //   conv = RAGSearchBox 대화 id — 그 대화에 올린 문서가 있으면 그것만 찾는다(문서 업로드 설계서)
    //   uploads = 올린 문서가 있던 대화인가 — 업로드 폴더가 사라졌으면 UPLOAD_MISSING 으로 답한다
    //------------------------------------------------------------------
    pub fn answer(&self, query: &str, top_k: Option<usize>, max_tokens: Option<usize>, folder: Option<&str>,
                  prev: Option<&str>, conv: Option<&str>, uploads: bool, on_event: &mut dyn FnMut(Event)) -> Result<(), String> {
        let t0 = Instant::now();
        // 따라 묻는 질문이면 검색에는 앞 질문을 붙이고, 모델에는 나눠 준다(멀티턴)
        let (search_q, gen_q, joined) = turn_queries(query, prev);
        // 이 대화에 올린 문서가 있으면 그것만 찾는다 — 절(Parent) 묶기는 인덱싱 폴더 인덱스 전용이라 쓰지 않는다
        let corpus = conv.and_then(|c| crate::uploads::corpus(c, &self.embedder));
        if corpus.is_none() && uploads {
            let mut t = Timing::new();
            t.insert("total_ms".into(), serde_json::json!(0.0));
            t.insert("upload_missing".into(), serde_json::json!(true));
            on_event(Event::Evidence(vec![], t.clone()));
            on_event(Event::Done(Done {
                answer: UPLOAD_MISSING.into(), answer_stream: UPLOAD_MISSING.into(), cited: vec![], cite_fixes: vec![],
                note: None, ttft_s: 0.0, llm_ttft_s: None, total_s: pyutil::round_to(t0.elapsed().as_secs_f64(), 2), timing: t,
            }));
            return Ok(());
        }
        let (chunks, mut timing) = if let Some(c) = &corpus {
            self.retriever.search_with(&search_q, top_k, None, Some(c))?
        } else if cfg().parent_mode == "section" {
            self.search_parents(&search_q, top_k, folder)?
        } else {
            self.retriever.search(&search_q, top_k, folder)?
        };
        if joined {
            timing.insert("multiturn".into(), serde_json::json!(true));
        }
        on_event(Event::Evidence(chunks.clone(), timing.clone()));
        // 근거를 못 찾았거나 리랭커가 확신하지 못한 질문은 동의어 후보 제안용으로 남긴다(동의어 설계서 3단계)
        //   업로드 문서 질문은 남기지 않는다 — 사용자 문서 낱말이 사전 후보에 섞이지 않게
        if corpus.is_none() {
            crate::synonym_suggest::log_miss(query, &timing);
        }
        let done_simple = |answer: &str| Done {
            answer: answer.to_string(), answer_stream: answer.to_string(), cited: vec![], cite_fixes: vec![],
            note: None, ttft_s: 0.0, llm_ttft_s: None,
            total_s: pyutil::round_to(t0.elapsed().as_secs_f64(), 2), timing: timing.clone(),
        };
        if chunks.is_empty() {
            let msg = if timing.get("no_relevant").and_then(|v| v.as_bool()).unwrap_or(false) {
                NO_RELEVANT
            } else if timing.get("scope_docs").and_then(|v| v.as_i64()) == Some(0) {
                NO_DOCS_IN_FOLDER
            } else {
                "검색된 근거가 없습니다."
            };
            on_event(Event::Done(done_simple(msg)));
            return Ok(());
        }
        let texts: Vec<String> = chunks.iter().map(|c| c.text.clone()).collect();
        let t_gen0 = Instant::now();
        let mut t_first: Option<Instant> = None;
        let out = self.generator.stream(&gen_q, &texts, max_tokens, None, &mut |piece| {
            if t_first.is_none() {
                t_first = Some(Instant::now());
            }
            on_event(Event::Token(piece.to_string()));
        })?;
        let t_end = Instant::now();
        // 되뱉기 거르기는 설정을 따른다(generation.echo_filter, 기본 끔)
        let (body, _) = parse_answer_opt(&out.text, Some(texts.len()), cfg().echo_filter);
        // 인용 번호 바로잡기(설계서 ⑤) — answer 는 다 고친 답, answer_stream 은 문장 끝 뒤 번호만 고친 답
        //   (스트리밍 화면은 문장 앞 번호를 이미 내보냈으니 화면 출력기는 이것으로 마무리한다)
        let (fixed, cite_fixes) = recite(&body, &texts, false, 0);
        let (answer_stream, _) = recite(&body, &texts, true, 0);
        let mut cited: Vec<usize> = CITE_NUM.captures_iter(&fixed).filter_map(|c| c[1].parse().ok())
            .filter(|n| (1..=texts.len()).contains(n)).collect();
        cited.sort();
        cited.dedup();
        let first = t_first.unwrap_or(t_end);
        on_event(Event::Done(Done {
            answer: fixed,
            answer_stream,
            cited,
            cite_fixes,
            note: out.note,
            ttft_s: pyutil::round_to((first - t0).as_secs_f64(), 2),
            llm_ttft_s: Some(pyutil::round_to((first - t_gen0).as_secs_f64(), 2)),
            total_s: pyutil::round_to((t_end - t0).as_secs_f64(), 2),
            timing,
        }));
        Ok(())
    }

    //------------------------------------------------------------------
    // 부모(절) 근거로 검색 — top_k 의 두 배를 받아 서로 다른 절로 top_k 개
    //------------------------------------------------------------------
    fn search_parents(&self, query: &str, top_k: Option<usize>, folder: Option<&str>) -> Result<(Vec<Chunk>, Timing), String> {
        let k = top_k.filter(|k| *k > 0).unwrap_or(cfg().top_k as usize);
        let (chunks, mut timing) = self.retriever.search(query, Some(k * 2), folder)?;
        if chunks.is_empty() {
            return Ok((chunks, timing));
        }
        let t = Instant::now();
        let tok = self.embedder.tokenizer()?;
        let tl = |s: &str| tok.tlen(s);
        let out = retrieve::expand(chunks, &self.store, &tl, k, cfg().parent_max_tokens as usize);
        timing.insert("parent_ms".into(), pyutil::jf(pyutil::round_to(t.elapsed().as_secs_f64() * 1000.0, 1)));
        Ok((out, timing))
    }
}

// 오류문을 "종류: 앞 200자" 로 줄이기
fn err_short(e: &str) -> String {
    pyutil::head(e, 220).to_string()
}

//------------------------------------------------------------------
// 응답 사건
//------------------------------------------------------------------
pub enum Event {
    Evidence(Vec<Chunk>, Timing),
    Token(String),
    Done(Done),
}

pub struct Done {
    pub answer: String,
    // 문장 끝 뒤 인용 번호만 바로잡은 답 — 스트리밍 화면 출력기가 마무리 기준으로 쓴다(설계서 ⑤)
    pub answer_stream: String,
    pub cited: Vec<usize>,
    // 바로잡은 인용 번호 (원래, 바꾼) — 소요 블록 "인용 근거" 줄에 함께 적는다
    pub cite_fixes: Vec<(usize, usize)>,
    // 답을 잘라 냈다는 표시(반복 루프 · 상한) — 본문 뒤에 따로 보여 준다
    pub note: Option<&'static str>,
    pub ttft_s: f64,
    pub llm_ttft_s: Option<f64>,
    pub total_s: f64,
    pub timing: Timing,
}
