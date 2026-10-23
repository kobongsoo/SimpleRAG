//------------------------------------------------------------------
// CLI 명령들 (Python cli.py)
//=> index / ask / chat / extract / search / clear / status / backend / warmup (+ Rust 판 import).
//   ask·chat 은 2단계 응답을 그대로 화면에 옮긴다 — 근거를 먼저 찍고 그 아래 답을 흘린다.
//   RAGSearchBox 가 읽는 줄 모양(질문> · ── 근거 · ── 답변 · ── 소요 · @index · @ev)은 Python 판과 같다.
//------------------------------------------------------------------

use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::app::{App, Event, NO_DOCS_IN_FOLDER, NO_RELEVANT};
use crate::commands;
use crate::config::{self, cfg};
use crate::generate::backend;
use crate::generate::prompts::strip_instruction_echo;
use crate::indexer;
use crate::pyutil;
use crate::retrieve::tget;

// Ctrl+C 가 눌렸나(생성 중이면 그 답변만 취소)
pub static INTERRUPT: AtomicBool = AtomicBool::new(false);

// 표준 출력에 쓰고 바로 내보낸다(파이프에서도 줄 단위로 보이게)
pub fn out(s: &str) {
    let mut o = std::io::stdout().lock();
    let _ = o.write_all(s.as_bytes());
    let _ = o.flush();
}
pub fn outln(s: &str) {
    out(&format!("{}\n", s));
}
// 진행 안내는 stderr(답변 출력에 섞이지 않게)
pub fn log(s: &str) {
    let mut e = std::io::stderr().lock();
    let _ = writeln!(e, "{}", s);
    let _ = e.flush();
}

//------------------------------------------------------------------
// 명령 인자
//------------------------------------------------------------------
#[derive(Debug, Default, Clone)]
pub struct Args {
    pub cmd: String,
    pub dir: Option<String>,
    pub rebuild: bool,
    pub no_recursive: bool,
    pub allow_delete: bool,
    pub max_add: Option<usize>,
    pub query: Option<String>,
    pub top_k: Option<usize>,
    pub max_tokens: Option<usize>,
    pub folder: Option<String>,
    pub model: Option<String>,
    pub precise: bool,
    pub no_stream: bool,
    pub path: Option<String>,
    pub chars: Option<usize>,
    pub blocks: bool,
    pub dump: Option<String>,
    pub doc: Option<String>,
    pub yes: bool,
    pub verbose: bool,
    pub reprobe: bool,
    pub from: Option<String>,
}

fn model_mode(alias: &str) -> String {
    match alias {
        config::FAST_GEN_MODEL => "빠름".into(),
        config::PRECISE_GEN_MODEL => "정밀".into(),
        config::KOREAN_GEN_MODEL => "한국어".into(),
        a => a.into(),
    }
}

// --precise / --model 고르기
fn pick_model(a: &Args) -> Result<Option<String>, String> {
    if a.precise {
        if let Some(m) = &a.model {
            if m != config::PRECISE_GEN_MODEL {
                return Err(format!("--precise 와 --model {} 를 함께 쓸 수 없습니다", m));
            }
        }
        return Ok(Some(config::PRECISE_GEN_MODEL.into()));
    }
    Ok(a.model.clone())
}

//------------------------------------------------------------------
// 백그라운드 리랭커 적재가 끝나면 stderr 로 한 줄 알리기(chat 전용)
//------------------------------------------------------------------
fn announce_rerank(app: &std::sync::Arc<App>) {
    let app = app.clone();
    std::thread::spawn(move || match app.wait_rerank() {
        Some(Ok(ms)) => log(&format!("리랭커 적재 완료 ({:.0}ms)", ms)),
        Some(Err(e)) => log(&format!("리랭커 적재 실패 — {}", e)),
        None => log("리랭커 적재 완료 (0ms)"),
    });
}

// ── index ─────────────────────────────────────────────────

pub fn cmd_index(a: &Args) -> i32 {
    let app = App::new(None);
    app.warmup(true, &mut |_| {}, Some(false));
    let dir = a.dir.clone().unwrap_or_default();
    let mut lg = |m: &str| outln(m);
    let r = indexer::index_folder(&dir, &app.embedder, &app.store, &app.bm25, a.rebuild, &mut lg,
                                  !a.no_recursive, a.allow_delete, a.max_add);
    let code = match r {
        Err(e) => {
            log(&format!("\n❌ {}", e));
            let res = obj(json!({"op": "index", "ok": false, "why": pyutil::head(&e, 200)}));
            outln(&commands::format_result(&res, "@index-summary "));
            1
        }
        Ok(s) => {
            let res = obj(json!({
                "files": s.files, "changed": s.changed, "added": s.c("added"), "modified": s.c("modified"),
                "same": s.c("same"), "moved": s.c("moved"), "copied": s.c("copied"), "removed": s.removed,
                "delete_blocked": s.delete_blocked, "held_new": s.held_new, "chunks": s.chunks,
                "sec": s.sec, "op": "index", "ok": true, "failed": s.failed.len(),
            }));
            outln(&commands::format_result(&res, "@index-summary "));
            outln(&format!("\n문서 {}건 / 추가 {}건 · 수정 {}건 · 내용 같음 {}건 · 이동 {}건 · 복사 {}건 · 삭제 {}건 / 청크 {}개 / {:.1}s",
                s.files, s.c("added"), s.c("modified"), s.c("same"), s.c("moved"), s.c("copied"), s.removed,
                pyutil::comma(s.chunks), s.sec));
            if !s.failed.is_empty() {
                outln(&format!("실패 {}건 (실패한 문서는 옛 버전이 그대로 남아 있습니다)", s.failed.len()));
            }
            if !s.delete_blocked.is_empty() {
                log(&format!("⚠️ 삭제를 멈췄습니다: {}. 정말 지운 것이면 --allow-delete 를 붙여 다시 실행하세요.", s.delete_blocked));
                3
            } else {
                0
            }
        }
    };
    app.close();
    code
}

fn obj(v: Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

// ── ask / chat 출력 ──────────────────────────────────────

//------------------------------------------------------------------
// 스트리밍 출력 버퍼 — 개행·40자·60ms 마다 내보내고 끝 90자는 붙들어 둔다(되뱉기 제거용)
//------------------------------------------------------------------
struct StreamWriter {
    min_chars: usize,
    interval: f64,
    hold: usize,
    fin: bool,
    pending: String,
    emitted: String,
    n: usize,
    last: Instant,
}

impl StreamWriter {
    fn new() -> Self {
        StreamWriter { min_chars: 40, interval: 0.06, hold: 90, fin: false, pending: String::new(), emitted: String::new(), n: 0, last: Instant::now() }
    }
    fn write(&mut self, text: &str) {
        self.pending.push_str(text);
        self.n += pyutil::clen(text);
        if text.contains('\n') || self.n >= self.min_chars || self.last.elapsed().as_secs_f64() >= self.interval {
            self.flush();
        }
    }
    fn flush(&mut self) {
        let keep = if self.fin { 0 } else { self.hold };
        let plen = pyutil::clen(&self.pending);
        let out_s = if keep > 0 { pyutil::head(&self.pending, plen.saturating_sub(keep)).to_string() } else { self.pending.clone() };
        if out_s.is_empty() {
            return;
        }
        self.pending = self.pending[out_s.len()..].to_string();
        self.emitted.push_str(&out_s);
        out(&out_s);
        self.n = 0;
        self.last = Instant::now();
    }
    // 마무리 — 되뱉기를 지운 뒤 남은 꼬리를 내보낸다(이미 나간 글자와 공백 무시로 이어질 때만)
    fn finish(&mut self, n_ev: Option<usize>) {
        self.fin = true;
        let full = format!("{}{}", self.emitted, self.pending);
        let cleaned = strip_instruction_echo(&full, n_ev);
        let tail = self.tail_after(&cleaned).or_else(|| Some(strip_instruction_echo(&self.pending, n_ev)));
        if let Some(t) = tail {
            self.pending = t;
        }
        self.flush();
    }
    fn tail_after(&self, cleaned: &str) -> Option<String> {
        let need: Vec<char> = self.emitted.chars().filter(|c| !pyutil::is_space(*c)).collect();
        let cs: Vec<char> = cleaned.chars().collect();
        let (mut i, mut k) = (0, 0);
        while k < need.len() && i < cs.len() {
            if !pyutil::is_space(cs[i]) {
                if cs[i] != need[k] {
                    return None;
                }
                k += 1;
            }
            i += 1;
        }
        if k < need.len() { None } else { Some(cs[i..].iter().collect()) }
    }
}

// 소요 블록의 "답변모델 … · parent …" 한 줄
fn answer_setup_line(app: &App) -> String {
    let alias = app.generator.alias.clone();
    let parent = if cfg().parent_mode == "section" { format!("section(상한 {}토큰)", cfg().parent_max_tokens) } else { cfg().parent_mode.clone() };
    format!("답변모델 {} ({}) · parent {}", alias, model_mode(&alias), parent)
}

//------------------------------------------------------------------
// 질의 1건 화면 출력 (ask·chat 공용) — true = 정상, false = 사용자 중단
//------------------------------------------------------------------
pub fn render_answer(app: &App, query: &str, top_k: Option<usize>, max_tokens: Option<usize>, stream: bool, folder: Option<&str>) -> bool {
    let mut writer = StreamWriter::new();
    let mut parts = String::new();
    let mut n_ev: Option<usize> = None;
    let mut early = false;
    let mut interrupted = false;
    INTERRUPT.store(false, Ordering::SeqCst);
    let r = app.answer(query, top_k, max_tokens, folder, &mut |ev| {
        if early {
            return;
        }
        match ev {
            Event::Evidence(chunks, timing) => {
                n_ev = Some(chunks.len());
                if folder.is_some() && timing.get("scope_docs").and_then(|v| v.as_i64()) == Some(0) {
                    outln(&format!("\n{}\n", NO_DOCS_IN_FOLDER));
                    early = true;
                    return;
                }
                if timing.get("no_relevant").and_then(|v| v.as_bool()).unwrap_or(false) {
                    outln(&format!("\n{}\n", NO_RELEVANT));
                    early = true;
                    return;
                }
                let scope = match folder {
                    Some(f) => {
                        let base = pyutil::basename(pyutil::rstrip_chars(f, "\\/"));
                        format!(" · 범위 {}(문서 {}건)", if base.is_empty() { f } else { base },
                                timing.get("scope_docs").map(|v| v.to_string()).unwrap_or("?".into()))
                    }
                    None => String::new(),
                };
                outln(&format!("\n── 근거 {}건 ({:.0}ms){} ─────────────────────", chunks.len(), tget(&timing, "total_ms"), scope));
                let full = std::env::var("SIMPLERAG_EVIDENCE_FULL").map(|v| v == "1").unwrap_or(false);
                for (i, c) in chunks.iter().enumerate() {
                    let body = pyutil::strip(&c.text.replace('\n', " ")).to_string();
                    outln(&format!("  [{}] {}", i + 1, c.doc_name));
                    outln(&format!("      {}{}", pyutil::head(&body, 160), if pyutil::clen(&body) > 160 { "…" } else { "" }));
                    if full {
                        let v = json!({"no": i + 1, "path": c.doc_path, "text": c.text});
                        outln(&format!("  @ev {}", pyutil::json_ascii(&v, false)));
                    }
                }
                outln("\n── 답변 (AI 요약 — 위 근거로 확인하세요) ──");
            }
            Event::Token(t) => {
                if INTERRUPT.load(Ordering::SeqCst) {
                    interrupted = true;
                    return;
                }
                if stream {
                    writer.write(&t);
                } else {
                    parts.push_str(&t);
                }
            }
            Event::Done(d) => {
                if stream {
                    writer.finish(n_ev);
                } else {
                    outln(pyutil::strip(&d.answer));
                }
                let t = &d.timing;
                outln("\n\n── 소요 ─────────────────────────────────");
                let rr = if t.contains_key("rerank_ms") { format!(" + 리랭킹 {:.0}", tget(t, "rerank_ms")) } else { String::new() };
                outln(&format!("  검색 {:.0}ms (임베딩 {:.0} + dense {:.0} + BM25 {:.0}{})",
                    tget(t, "total_ms"), tget(t, "embed_ms"), tget(t, "dense_ms"), tget(t, "bm25_ms"), rr));
                if let Some(e) = t.get("rerank_error").and_then(|v| v.as_str()) {
                    outln(&format!("  ⚠️ 리랭킹 실패 — RRF 순서로 답했습니다: {}", e));
                }
                outln(&format!("  첫 글자 {:.2}s / 완료 {:.2}s", d.ttft_s, d.total_s));
                outln(&format!("  {}", answer_setup_line(app)));
                if !d.cited.is_empty() {
                    outln(&format!("  인용 근거: {}", d.cited.iter().map(|n| format!("[{}]", n)).collect::<Vec<_>>().join(", ")));
                }
            }
        }
    });
    if let Err(e) = r {
        writer.finish(n_ev);
        log(&format!("\n오류: {}", e));
    }
    if interrupted {
        writer.finish(n_ev);
        outln("\n\n[중단됨]");
        return false;
    }
    true
}

pub fn cmd_ask(a: &Args) -> i32 {
    if a.query.as_deref().map(|q| q.is_empty()).unwrap_or(true) {
        return cmd_chat(a);
    }
    let model = match pick_model(a) {
        Ok(m) => m,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let app = App::new(model.as_deref());
    app.warmup(false, &mut |m| log(m), None);
    app.wait_rerank();
    let ok = render_answer(&app, a.query.as_deref().unwrap(), a.top_k, a.max_tokens, !a.no_stream, a.folder.as_deref());
    app.close();
    if ok { 0 } else { 130 }
}

//------------------------------------------------------------------
// stdin 한 줄 — UTF-8 이 아니면 cp949 로 푼다(RAGSearchBox 는 cp949 로 보낸다)
//------------------------------------------------------------------
fn read_line() -> Option<String> {
    let mut buf = vec![];
    match std::io::stdin().lock().read_until(b'\n', &mut buf) {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            while buf.last().map(|b| *b == b'\n' || *b == b'\r').unwrap_or(false) {
                buf.pop();
            }
            Some(match std::str::from_utf8(&buf) {
                Ok(s) => s.to_string(),
                Err(_) => encoding_rs::EUC_KR.decode(&buf).0.into_owned(),
            })
        }
    }
}

//------------------------------------------------------------------
// 대화형 명령 (워커) — 모델을 한 번 올리고 질문을 계속 받는다
//------------------------------------------------------------------
pub fn cmd_chat(a: &Args) -> i32 {
    let model = match pick_model(a) {
        Ok(m) => m,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let app = std::sync::Arc::new(App::new(model.as_deref()));
    let mut top_k = a.top_k;
    let mut rr_warned = false;
    let mut chat_folder: Option<String> = None;
    outln("모델 적재 중...");
    let warm = app.warmup(false, &mut |m| log(m), None);
    let count = app.store.stats().map(|s| s.0).unwrap_or(0);
    let alias = model.clone().unwrap_or_else(|| cfg().default_gen_model.clone());
    // 구성요소가 실패했으면 stderr 로 알린다(RAGSearchBox 가 꼬리를 보여 준다)
    for (k, v) in &warm.items {
        if let Some(e) = v.as_str() {
            log(&format!("[warmup] {} 실패 — {}", k, e));
        }
    }
    outln(&format!("준비 완료 ({:.1}초) — {}청크 / {} ({} 모드)", warm.total_ms / 1000.0, pyutil::comma(count as i64),
        config::gen_file(&alias).unwrap_or(&alias), model_mode(&alias)));
    outln(&format!("생성 {} / 리랭킹 {}", backend::label(&warm.backend),
        if warm.rerank { if warm.rerank_deferred { "켬(백그라운드 적재 중)" } else { "켬" } } else { "끔" }));
    if warm.rerank && warm.rerank_deferred {
        announce_rerank(&app);
    }
    outln(&format!("모델: 임베딩 {} / 리랭커 {}", pyutil::basename(pyutil::rstrip_chars(&cfg().model_dir, "\\/")),
        pyutil::basename(pyutil::rstrip_chars(&cfg().rerank_dir, "\\/"))));
    outln("기능: ask-folder index-commands");
    outln("질문을 입력하세요.  종료: exit 또는 Ctrl+D   도움말: /help\n");
    loop {
        out("질문> ");
        let line = match read_line() {
            Some(l) => l,
            None => {
                outln("");
                break;
            }
        };
        let mut query = pyutil::strip(&line).to_string();
        if query.is_empty() {
            continue;
        }
        if ["exit", "quit", "종료", "/exit", "/quit"].contains(&query.to_lowercase().as_str()) {
            break;
        }
        let mut ask_folder = chat_folder.clone();
        if query.starts_with("/ask ") || query == "/ask" {
            let raw = pyutil::strip(&query[4..]).to_string();
            let raw = if raw.is_empty() { "{}".to_string() } else { raw };
            match serde_json::from_str::<Value>(&raw) {
                Ok(Value::Object(req)) => {
                    query = pyutil::strip(&req.get("q").map(|v| v.as_str().map(|s| s.to_string()).unwrap_or_else(|| v.to_string())).unwrap_or_default()).to_string();
                    ask_folder = req.get("folder").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| s.to_string());
                }
                _ => {
                    outln("  /ask 형식이 잘못됐습니다: /ask {\"q\": \"질문\", \"folder\": \"폴더\"}\n");
                    continue;
                }
            }
            if query.is_empty() {
                continue;
            }
        } else if query.starts_with('/') {
            let parts: Vec<&str> = pyutil::split_ws(&query);
            let cmd = parts[0].to_lowercase();
            let rest = pyutil::strip(&query[parts[0].len()..]).to_string();
            if cmd == "/help" {
                outln(&format!("  /topk N     근거 개수 변경 (현재 {})", top_k.unwrap_or(cfg().top_k as usize)));
                outln("  /status     인덱스 상태");
                outln("  /folder <폴더>       이후 질문은 그 폴더(하위 포함) 문서에서만 찾기. 비우면 해제");
                outln("  /index-doc <경로>    문서 한 건 반영(추가·수정)");
                outln("  /remove-doc <경로>   지운 문서를 인덱스에서 빼기");
                outln("  /bm25                키워드 색인 다시 만들기");
                outln("  /index-plan <폴더>   폴더와 인덱스 비교(바꾸지 않음)");
                outln("  exit        종료\n");
            } else if cmd == "/topk" && parts.len() > 1 && parts[1].chars().all(|c| c.is_ascii_digit()) && !parts[1].is_empty() {
                top_k = parts[1].parse().ok();
                outln(&format!("  근거 개수를 {}개로 변경했습니다.\n", parts[1]));
            } else if cmd == "/status" {
                let (n, mb) = app.store.stats().unwrap_or((0, 0.0));
                outln(&format!("  {}청크 / {:.1}MB\n", pyutil::comma(n as i64), mb));
            } else if cmd == "/folder" {
                let arg = pyutil::strip_chars(&rest, "\"").to_string();
                chat_folder = if arg.is_empty() { None } else { Some(arg) };
                outln(&format!("  검색 범위: {}\n", chat_folder.as_deref().unwrap_or("인덱스 전체")));
            } else if commands::COMMANDS.contains(&cmd.as_str()) {
                let res = commands::run(&app, &cmd, &rest);
                outln(&commands::format_result(&res, commands::PREFIX));
            } else {
                outln("  알 수 없는 명령입니다. /help 를 입력하세요.\n");
            }
            continue;
        }
        render_answer(&app, &query, top_k, a.max_tokens, !a.no_stream, ask_folder.as_deref());
        outln("");
        if let Some(Err(e)) = app.rerank_load.lock().unwrap().clone() {
            if !rr_warned {
                outln(&format!("  ⚠️ 리랭커 적재 실패 — 리랭킹 없이 답합니다: {}\n", e));
                rr_warned = true;
            }
        }
    }
    outln("종료합니다.");
    app.close();
    0
}

// ── extract ───────────────────────────────────────────────

pub fn cmd_extract(a: &Args) -> i32 {
    let path = a.path.clone().unwrap_or_default();
    if !std::path::Path::new(&path).is_file() {
        log(&format!("파일 없음: {}", path));
        return 2;
    }
    let chars = a.chars.unwrap_or(1200);
    if a.blocks {
        return extract_blocks_preview(&path, chars, a.dump.as_deref());
    }
    let (text, n_rows) = match crate::extract::extract_text(&path, true) {
        Ok(x) => x,
        Err(e) => {
            log(&format!("추출 실패: ExtractError: {}", e));
            return 1;
        }
    };
    if text.is_empty() {
        outln("추출 텍스트 없음 — 스캔 이미지 PDF 이거나 지원하지 않는 포맷입니다.");
        return 1;
    }
    outln(&format!("파일    : {}", pyutil::basename(&path)));
    outln(&format!("문자수  : {}", pyutil::comma(pyutil::clen(&text) as i64)));
    outln(&if n_rows > 0 { format!("표 복원 : {}행", n_rows) } else { "표 복원 : 없음".into() });
    let emb = crate::embed::Embedder::new();
    let tok = match emb.tokenizer() {
        Ok(t) => t,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let chunks = crate::chunk::split_text(&text, &tok, None, None);
    outln(&format!("청크수  : {}개 ({}토큰 기준)", chunks.len(), cfg().chunk_tokens));
    outln(&"-".repeat(66));
    outln(pyutil::head(&text, chars));
    let n = pyutil::clen(&text);
    if n > chars {
        outln(&format!("\n... (이하 {}자 생략)", pyutil::comma((n - chars) as i64)));
    }
    0
}

// extract --blocks — 블록·절·청크 미리보기
fn extract_blocks_preview(path: &str, chars: usize, dump: Option<&str>) -> i32 {
    let mut doc = match crate::docmodel::extract_document(path, None) {
        Ok(d) => d,
        Err(e) => {
            log(&format!("추출 실패: ExtractError: {}", e));
            return 1;
        }
    };
    outln(&format!("파일    : {}", pyutil::basename(path)));
    outln(&format!("경로    : {}", if doc.is_blocks() { format!("블록 ({})", doc.format) } else { "폴백 — 기존 추출 + 정규식 절 추측".into() }));
    for w in &doc.warnings {
        outln(&format!("경고    : {}", w));
    }
    if let Some(d) = dump {
        if let Ok(p) = crate::docmodel::dump_json(&doc, d, pyutil::basename(path)) {
            outln(&format!("블록JSON: {}", p));
        }
    }
    if doc.is_blocks() {
        let mut kinds: Vec<(String, usize)> = vec![];
        for b in &doc.blocks {
            let k = format!("{}/{}", b.kind, b.source);
            match kinds.iter_mut().find(|x| x.0 == k) {
                Some(x) => x.1 += 1,
                None => kinds.push((k, 1)),
            }
        }
        outln(&format!("블록    : {}개  {{{}}}", doc.blocks.len(), kinds.iter().map(|(k, n)| format!("'{}': {}", k, n)).collect::<Vec<_>>().join(", ")));
        let secs = crate::docmodel::assemble::build_sections(&mut doc);
        outln(&format!("절      : {}개", secs.len()));
        for s in secs.iter().take(30) {
            let pages = match s.page_from { Some(a) => format!(" p{}-{}", a, s.page_to.unwrap_or(a)), None => String::new() };
            let path_s = if s.path.is_empty() { "(머리말)".to_string() } else { s.path.join(" > ") };
            outln(&format!("   [{:>3}] {}{} · 블록 {}", s.id, path_s, pages, s.blocks.len()));
        }
        if secs.len() > 30 {
            outln(&format!("   ... (이하 {}개 생략)", secs.len() - 30));
        }
    }
    let emb = crate::embed::Embedder::new();
    let tok = match emb.tokenizer() {
        Ok(t) => t,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let chunks = crate::chunk::split_blocks(&mut doc, &tok, None);
    let mut lens: Vec<usize> = chunks.iter().map(|c| tok.tlen(&c.text)).collect();
    lens.sort();
    if lens.is_empty() {
        outln("청크    : 없음");
    } else {
        outln(&format!("청크    : {}개 (토큰 최소 {} · 중앙 {} · 최대 {}, 예산 {})", chunks.len(), lens[0], lens[lens.len() / 2], lens[lens.len() - 1], cfg().chunk_tokens));
    }
    outln(&"-".repeat(66));
    let mut shown = 0;
    for (i, c) in chunks.iter().enumerate() {
        if shown >= chars {
            outln(&format!("\n... (청크 {}개 더)", chunks.len() - i));
            break;
        }
        outln(&format!("── 청크 {} ──", i));
        outln(&c.text);
        shown += pyutil::clen(&c.text);
    }
    0
}

// ── search ────────────────────────────────────────────────

pub fn cmd_search(a: &Args) -> i32 {
    let app = App::new(None);
    app.warmup(true, &mut |_| {}, None);
    app.wait_rerank();
    let q = a.query.clone().unwrap_or_default();
    let chars = a.chars.unwrap_or(200);
    let r = app.retriever.search(&q, a.top_k, a.folder.as_deref());
    let code = match r {
        Err(e) => {
            log(&format!("검색 실패: {}", e));
            1
        }
        Ok((chunks, t)) => {
            outln(&format!("\n질의: {}", q));
            if let Some(f) = &a.folder {
                outln(&format!("범위: {} (문서 {}건, 벡터 한 벌)", f, t.get("scope_docs").map(|v| v.to_string()).unwrap_or_default()));
            }
            let rr = if t.contains_key("rerank_ms") { format!(" + 리랭킹 {:.0}", tget(&t, "rerank_ms")) } else { String::new() };
            outln(&format!("검색 {:.0}ms (임베딩 {:.0} + dense {:.0} + BM25 {:.0} + 융합 {:.0}{})\n",
                tget(&t, "total_ms"), tget(&t, "embed_ms"), tget(&t, "dense_ms"), tget(&t, "bm25_ms"), tget(&t, "fuse_ms"), rr));
            if chunks.is_empty() {
                if a.folder.is_some() && t.get("scope_docs").and_then(|v| v.as_i64()) == Some(0) {
                    outln("이 폴더(하위 포함)에는 인덱싱된 문서가 없습니다.");
                } else {
                    outln("검색 결과 없음 — 인덱스가 비었거나 질의어가 코퍼스에 없습니다.");
                }
                1
            } else {
                for (i, c) in chunks.iter().enumerate() {
                    outln(&format!("[{}] {}  (폴더: {})", i + 1, c.doc_name, if c.folder.is_empty() { "-" } else { &c.folder }));
                    let body = pyutil::strip(&c.text.replace('\n', " ")).to_string();
                    outln(&format!("    {}{}\n", pyutil::head(&body, chars), if pyutil::clen(&body) > chars { "…" } else { "" }));
                }
                0
            }
        }
    };
    app.close();
    code
}

// ── status ────────────────────────────────────────────────

pub fn cmd_status(a: &Args) -> i32 {
    let app = App::new(None);
    let (n, mb) = match app.store.stats() {
        Ok(s) => s,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let state = indexer::load_state();
    let c = cfg();
    outln(&format!("인덱스   : {}청크 / {:.1}MB", pyutil::comma(n as i64), mb));
    outln(&format!("문서     : {}건", indexer::docs(&state).len()));
    outln(&format!("BM25캐시 : {}", if std::path::Path::new(&c.bm25_path).is_file() { "있음" } else { "없음(dense 단독 폴백)" }));
    outln(&format!("설정파일 : {}", c.config_path.as_deref().unwrap_or("없음(코드 기본값)")));
    outln(&format!("청킹     : {}토큰 / 겹침 {} / {} / 머리글 상한 {}", c.chunk_tokens, c.chunk_overlap, c.chunk_mode, c.table_header_max_tokens));
    let rec = state.get("chunk_params").filter(|v| !v.is_null()).cloned();
    let built = match &rec {
        Some(r) => r.get("extract_engine").and_then(|v| v.as_str()).unwrap_or("legacy").to_string(),
        None => "기록 없음".into(),
    };
    outln(&format!("추출엔진 : {} (인덱스: {})", c.extract_engine, built));
    if !indexer::docs(&state).is_empty() && rec.is_none() {
        outln("  ⚠️ 인덱스에 청킹 설정 기록 없음(이전 버전에서 만듦)");
    } else if let Some(r) = &rec {
        let mut tmp = state.clone();
        tmp.insert("docs".into(), json!({"x": 1}));
        if indexer::check_chunk_params(&mut tmp, false, &mut |_| {}).is_err() {
            outln(&format!("  ⚠️ 인덱스는 다른 청킹 설정으로 만들어짐 — --rebuild 필요: {}", indexer::show_params(r)));
        }
    }
    outln(&format!("검색     : dense {} + BM25 {} → RRF(k={}) → 리랭커 입력 {} → sLLM 근거 {}건",
        c.dense_top_k, c.bm25_top_k, fmt_g(c.rrf_k), c.rerank_pool, c.top_k));
    let others: Vec<&str> = config::USER_GEN_MODELS.iter().copied().filter(|m| *m != c.default_gen_model).collect();
    outln(&format!("생성모델 : {} ({} 모드)   ← {}", config::gen_file(&c.default_gen_model).unwrap_or(""), model_mode(&c.default_gen_model),
        if c.default_gen_model == config::FAST_GEN_MODEL { "한 번만 정밀하게: ask --precise".to_string() } else { format!("다른 모델: ask --model {}", others.join(" | ")) }));
    match backend::peek(None) {
        Some(cached) => outln(&format!("생성백엔드: {} — {}", backend::label(cached["backend"].as_str().unwrap_or("cpu")), cached.get("reason").and_then(|v| v.as_str()).unwrap_or(""))),
        None => outln("생성백엔드: 미측정 — 첫 질문 때 자동 측정(`backend` 명령으로 미리 가능)"),
    }
    outln(&format!("리랭커   : {}", if std::path::Path::new(&pyutil::join(&c.rerank_dir, "model_int8.onnx")).is_file() { "있음" } else { "없음(리랭킹 비활성)" }));
    if a.verbose {
        outln("\n문서별 청크 수:");
        let mut v: Vec<(&String, &Value)> = indexer::docs(&state).iter().collect();
        v.sort_by_key(|(_, m)| std::cmp::Reverse(m.get("chunks").and_then(|x| x.as_i64()).unwrap_or(0)));
        for (p, m) in v {
            outln(&format!("  {:>5}  {}", m.get("chunks").and_then(|x| x.as_i64()).unwrap_or(0), m.get("name").and_then(|x| x.as_str()).unwrap_or(p)));
        }
    }
    app.close();
    0
}

// 파이썬 "{:g}" 흉내
fn fmt_g(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e16 { format!("{}", f as i64) } else { format!("{}", f) }
}

// ── clear ─────────────────────────────────────────────────

fn ask_yes(prompt: &str) -> bool {
    out(prompt);
    matches!(read_line().map(|s| pyutil::strip(&s).to_lowercase()).as_deref(), Some("y") | Some("yes"))
}

pub fn cmd_clear(a: &Args) -> i32 {
    let c = cfg();
    if let Some(doc) = &a.doc {
        let target = pyutil::abspath(doc);
        let mut state = indexer::load_state();
        let meta = indexer::docs(&state).get(&target).cloned();
        let meta = match meta {
            Some(m) => m,
            None => {
                log(&format!("인덱스에 없는 문서입니다: {}", target));
                log("  (경로가 인덱싱 당시와 정확히 같아야 합니다. `status -v` 로 확인하세요)");
                return 1;
            }
        };
        if !a.yes {
            outln(&format!("다음 문서의 청크 {}개를 인덱스에서 제거합니다:", meta.get("chunks").map(|v| v.to_string()).unwrap_or("?".into())));
            outln(&format!("  {}", target));
            if !ask_yes("진행할까요? [y/N] ") {
                outln("취소했습니다.");
                return 1;
            }
        }
        let app = App::new(None);
        if let Err(e) = app.store.ensure_loaded().and_then(|_| indexer::remove_doc(&target, &mut state, &app.store)) {
            log(&e);
            return 1;
        }
        outln("제거했습니다. BM25 는 다음 `index --dir <폴더>` 때 다시 만들어집니다 (그 전에도 지운 문서는 근거로 나오지 않습니다).");
        app.close();
        return 0;
    }
    let mut targets: Vec<(String, &str, u64)> = vec![];
    for (p, label) in [(&c.index_dir, "벡터 인덱스"), (&c.bm25_path, "BM25 캐시"), (&c.state_path, "증분 상태")] {
        let path = std::path::Path::new(p);
        if path.is_dir() {
            let size = std::fs::read_dir(path).map(|rd| rd.flatten().map(|e| e.metadata().map(|m| m.len()).unwrap_or(0)).sum()).unwrap_or(0);
            targets.push((p.clone(), label, size));
        } else if path.is_file() {
            targets.push((p.clone(), label, std::fs::metadata(path).map(|m| m.len()).unwrap_or(0)));
        }
    }
    if targets.is_empty() {
        outln(&format!("삭제할 인덱스가 없습니다. (데이터 루트: {})", c.root));
        return 0;
    }
    let state = indexer::load_state();
    outln(&format!("데이터 루트: {}", c.root));
    outln(&format!("문서 {}건 / 다음 항목을 삭제합니다:", indexer::docs(&state).len()));
    for (p, label, size) in &targets {
        outln(&format!("  {:<12} {:>8.1} MB  {}", label, *size as f64 / 1024.0 / 1024.0, pyutil::basename(p)));
    }
    if !a.yes {
        outln("\n⚠️ 되돌릴 수 없습니다. 다시 인덱싱하려면 문서 수에 따라 수십 분이 걸립니다.");
        if !ask_yes("정말 삭제할까요? [y/N] ") {
            outln("취소했습니다.");
            return 1;
        }
    }
    let mut failed = vec![];
    for (p, label, _) in &targets {
        let path = std::path::Path::new(p);
        let r = if path.is_dir() { std::fs::remove_dir_all(path) } else { std::fs::remove_file(path) };
        match r {
            Ok(_) => outln(&format!("  삭제: {}", label)),
            Err(e) => failed.push((label, pyutil::head(&e.to_string(), 80).to_string())),
        }
    }
    if !failed.is_empty() {
        log("\n일부 삭제 실패 — 다른 프로세스가 사용 중일 수 있습니다:");
        for (l, w) in failed {
            log(&format!("  {}: {}", l, w));
        }
        return 1;
    }
    outln("\n삭제 완료. `index --dir <폴더>` 로 다시 인덱싱하세요.");
    0
}

// ── backend / warmup ──────────────────────────────────────

pub fn cmd_backend(a: &Args) -> i32 {
    let info = backend::select(None, a.reprobe, &mut |m| outln(m));
    let src = match info.get("source").and_then(|v| v.as_str()).unwrap_or("") {
        "cache" => "저장된 측정", "probe" => "방금 측정", "config" => "환경변수 지정", "check" => "사전 점검", "loaded" => "이미 적재됨", _ => "-",
    };
    let b = info["backend"].as_str().unwrap_or("cpu").to_string();
    outln(&format!("\n생성 백엔드 : {}", backend::label(&b)));
    outln(&format!("선택 근거   : {} — {}", src, info.get("reason").and_then(|v| v.as_str()).unwrap_or("")));
    for (kind, lab) in [("cpu", "CPU"), ("vulkan", "iGPU")] {
        let m = match info.get(kind) {
            Some(m) if m.is_object() => m,
            _ => continue,
        };
        if m.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
            let f = |k: &str| m.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
            outln(&format!("  {:<5} prefill {:>5.0} t/s  ({}토큰 {:.2}초 / 적재 {:.1}초 / 첫회 {:.1}초)",
                lab, f("prefill_tps"), m.get("prompt_tokens").and_then(|v| v.as_i64()).unwrap_or(0), f("prefill_s"), f("load_s"), f("warm_s")));
        } else {
            outln(&format!("  {:<5} 사용 불가 — {}", lab, m.get("error").and_then(|v| v.as_str()).unwrap_or("")));
        }
    }
    let use_rr = backend::rerank_enabled(&b);
    let has_rr = std::path::Path::new(&pyutil::join(&cfg().rerank_dir, "model_int8.onnx")).is_file();
    outln(&format!("리랭킹      : {}{}", if use_rr { "켬" } else { "끔" }, if use_rr && !has_rr { " (⚠️ 모델 없음 → 실제로는 끔)" } else { "" }));
    outln(&format!("저장 위치   : {}", cfg().backend_cache_path));
    outln("\n강제 지정: 환경변수 SIMPLERAG_GEN_BACKEND=cpu|vulkan, SIMPLERAG_RERANK=1|0");
    outln("다시 측정: backend --reprobe");
    0
}

pub fn cmd_warmup(a: &Args) -> i32 {
    let model = match pick_model(a) {
        Ok(m) => m,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let app = App::new(model.as_deref());
    let r = app.warmup(false, &mut |m| log(m), None);
    outln(&format!("  생성      {} / 리랭킹 {}", backend::label(&r.backend), if r.rerank { "켬" } else { "끔" }));
    for k in ["embedder", "qdrant", "bm25", "reranker", "llm", "llm_warm"] {
        match r.items.get(k) {
            None => continue,
            Some(Value::String(s)) => outln(&format!("  {:<9} ❌ {}", k, s)),
            Some(v) => {
                let f = v.as_f64().unwrap_or(0.0);
                if f < 0.0 { outln(&format!("  {:<9} — 캐시 없음", k)) } else { outln(&format!("  {:<9} {:.0}ms", k, f)) }
            }
        }
    }
    outln(&format!("  {:<9} {:.0}ms (병렬 총합 = 준비 완료)", "합계", r.total_ms));
    if r.rerank_deferred {
        match app.wait_rerank() {
            Some(Err(e)) => outln(&format!("  {:<9} ❌ {} (백그라운드)", "reranker", e)),
            Some(Ok(ms)) => outln(&format!("  {:<9} {:.0}ms (준비 완료 뒤 백그라운드)", "reranker", ms)),
            None => outln(&format!("  {:<9} 0ms (준비 완료 뒤 백그라운드)", "reranker")),
        }
    }
    app.close();
    0
}

// ── import (Rust 판 전용) ─────────────────────────────────

//------------------------------------------------------------------
// Python 판 인덱스 가져오기
//=> <폴더>/qdrant_data/collection/rag/storage.sqlite 의 점(pickle)과 index_state.json 을 읽어
//   Rust 판 저장소·상태 파일로 옮기고 BM25 를 새로 만든다. 다시 임베딩하지 않는다.
//   Python 판이 실행 중이어도 읽을 수 있게 sqlite 를 임시 사본으로 떠서 읽는다.
//------------------------------------------------------------------
pub fn cmd_import(a: &Args) -> i32 {
    let src = pyutil::abspath(&a.from.clone().unwrap_or_default());
    let sqlite = pyutil::join(&pyutil::join(&pyutil::join(&pyutil::join(&src, "qdrant_data"), "collection"), "rag"), "storage.sqlite");
    let state_src = pyutil::join(&src, "index_state.json");
    if !std::path::Path::new(&sqlite).is_file() || !std::path::Path::new(&state_src).is_file() {
        log(&format!("가져올 Python 판 인덱스가 없습니다: {}", src));
        log("  (qdrant_data\\collection\\rag\\storage.sqlite 와 index_state.json 이 있어야 합니다)");
        return 2;
    }
    let tmp = std::env::temp_dir().join(format!("simplerag_import_{}.sqlite", std::process::id()));
    if let Err(e) = std::fs::copy(&sqlite, &tmp) {
        log(&format!("사본을 만들 수 없습니다: {}", e));
        return 1;
    }
    let t0 = Instant::now();
    let conn = match rusqlite::Connection::open(&tmp) {
        Ok(c) => c,
        Err(e) => {
            log(&e.to_string());
            return 1;
        }
    };
    let mut ids = vec![];
    let mut vecs = vec![];
    let mut pls = vec![];
    let mut bad = 0;
    {
        let mut st = conn.prepare("SELECT point FROM points").unwrap();
        let rows = st.query_map([], |r| r.get::<_, Vec<u8>>(0)).unwrap();
        for b in rows.flatten() {
            match crate::pickle::point_of(&b) {
                Ok((id, v, p)) => {
                    ids.push(id);
                    vecs.push(v);
                    pls.push(p);
                }
                Err(_) => bad += 1,
            }
        }
    }
    drop(conn);
    let _ = std::fs::remove_file(&tmp);
    outln(&format!("Python 판 점 {}개 읽음{} ({:.1}s)", pyutil::comma(ids.len() as i64),
        if bad > 0 { format!(" · 읽지 못함 {}개", bad) } else { String::new() }, t0.elapsed().as_secs_f64()));
    let app = App::new(None);
    if let Err(e) = app.store.recreate().and_then(|_| app.store.upsert(&ids, &vecs, &pls)) {
        log(&e);
        return 1;
    }
    let raw = std::fs::read(&state_src).unwrap_or_default();
    let state: indexer::State = serde_json::from_slice::<Value>(&raw).ok().and_then(|v| v.as_object().cloned()).unwrap_or_else(indexer::empty_state);
    let mut state = state;
    if let Err(e) = indexer::save_state(&state) {
        log(&e);
        return 1;
    }
    for f in ["gen_backend.json"] {
        let s = pyutil::join(&src, f);
        let d = pyutil::join(&cfg().root, f);
        if std::path::Path::new(&s).is_file() && !std::path::Path::new(&d).is_file() {
            let _ = std::fs::copy(&s, &d);
        }
    }
    outln("BM25 인덱스 구축 중...");
    let tok = match app.embedder.tokenizer() {
        Ok(t) => t,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let (texts, bids, _) = indexer::collect_for_bm25(&app.store, None, &mut |m| outln(m)).unwrap_or_default();
    match app.bm25.build(&texts, &bids, &tok) {
        Ok(s) => outln(&format!("BM25 인덱스 {}건 구축 {:.1}s", pyutil::comma(texts.len() as i64), s)),
        Err(e) => log(&e),
    }
    state.insert("bm25_built_at".into(), pyutil::jf(pyutil::now_ts()));
    let _ = indexer::save_state(&state);
    outln(&format!("\n가져오기 완료 — 문서 {}건 / 청크 {}개 → {}", indexer::docs(&state).len(), pyutil::comma(ids.len() as i64), cfg().root));
    app.close();
    0
}
