//------------------------------------------------------------------
// SimpleRAG 워커 수명 관리 + 출력 해석 (Python rag_worker.py · chat_parser.py)
//=> `simplerag-rs.exe chat` 을 콘솔 없이 띄워 모델을 올려 둔 채로 두고, 질문을 표준입력으로
//   한 줄씩 넣는다. 출력은 "질문> " 프롬프트와 ── 근거 / ── 답변 / ── 소요 블록으로 나눠 읽는다.
//    · 프롬프트가 보인 뒤에만 다음 질문·명령을 보낸다(출력이 섞이지 않게)
//    · 인덱싱 명령(/index-doc 등)은 질문 사이사이에 하나씩 보내고 "@index {json}" 결과를 받는다
//    · 죽으면 다시 띄우되(5분에 restart_max 번까지), 설정 오류(코드 2)·인덱스 잠금이면 멈춘다
//    · 답이 멈추면(answer_timeout) 죽이고 다시 올린다, 오래 쉬면(idle_unload) 내린다
//------------------------------------------------------------------

use std::collections::{HashSet, VecDeque};
use std::io::{Read, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

pub const STOPPED: &str = "stopped";
pub const STARTING: &str = "starting";
pub const READY: &str = "ready";
pub const BUSY: &str = "busy";
pub const INDEXING: &str = "indexing";

const INDEX_PREFIX: &str = "@index ";
const CMD_TIMEOUT_S: u64 = 300;
const RESTART_WINDOW_S: u64 = 300;
const LOCK_HINT: &str = "already accessed";
const RERANK_DONE: &str = "리랭커 적재 완료";
const RERANK_FAIL: &str = "리랭커 적재 실패";
const RERANK_LOADING: &str = "켬(백그라운드 적재 중)";

pub const PROMPT: &str = "질문> ";
const EVIDENCE_MARK: &str = "── 근거 ";
const ANSWER_MARK: &str = "── 답변 ";
const TIMING_MARK: &str = "── 소요";

//------------------------------------------------------------------
// 워커가 앱에 알리는 일
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub enum WEvent {
    State(String, String),
    Evidence(Vec<Value>, i64),
    Token(String),
    Done(Value),
    Raw(String),
    Error(String),
    Note(String, String),
}

// ── 출력 해석기 ────────────────────────────────────────

//------------------------------------------------------------------
// 해석기 상태 — 시작 중 → 대기 → 근거 → 답변 → 소요 → 대기 …
//------------------------------------------------------------------
pub struct ChatParser {
    state: &'static str,
    buf: String,
    evidence: Vec<Value>,
    ev_ms: i64,
    answer: String,
    timing_text: String,
    saw_evidence: bool,
}

impl ChatParser {
    pub fn new() -> Self {
        ChatParser { state: "starting", buf: String::new(), evidence: vec![], ev_ms: 0, answer: String::new(), timing_text: String::new(), saw_evidence: false }
    }

    // 질문 하나를 보낼 때 — 그 질문의 모음을 비운다
    pub fn begin_question(&mut self) {
        self.state = "idle";
        self.evidence.clear();
        self.ev_ms = 0;
        self.answer.clear();
        self.timing_text.clear();
        self.saw_evidence = false;
    }

    //------------------------------------------------------------------
    // 출력 조각 먹이기 → 해석된 사건들
    //------------------------------------------------------------------
    pub fn feed(&mut self, text: &str) -> Vec<(String, Value)> {
        self.buf.push_str(text);
        let mut out = vec![];
        while self.step(&mut out) {}
        out
    }

    fn step(&mut self, out: &mut Vec<(String, Value)>) -> bool {
        match self.state {
            "starting" => {
                let i = match self.buf.find(PROMPT) {
                    Some(i) => i,
                    None => return false,
                };
                self.buf = self.buf[i + PROMPT.len()..].to_string();
                self.state = "idle";
                out.push(("ready".into(), Value::Null));
                true
            }
            "idle" => {
                if let Some(i) = self.buf.find(EVIDENCE_MARK) {
                    self.buf = self.buf[i..].to_string();
                    self.state = "evidence";
                    self.saw_evidence = true;
                    return true;
                }
                if let Some(j) = self.buf.find(PROMPT) {
                    let raw = self.buf[..j].trim().to_string();
                    self.buf = self.buf[j + PROMPT.len()..].to_string();
                    if !raw.is_empty() {
                        out.push(("raw".into(), json!(raw)));
                    }
                    out.push(("done".into(), self.done_info()));
                    return true;
                }
                false
            }
            "evidence" => {
                let k = match self.buf.find(ANSWER_MARK) {
                    Some(k) => k,
                    None => return false,
                };
                let rest = &self.buf[k..];
                let nl = match rest.find('\n') {
                    Some(n) => n,
                    None => return false,
                };
                let block = self.buf[..k].to_string();
                self.buf = rest[nl + 1..].to_string();
                self.parse_evidence(&block);
                self.state = "answer";
                out.push(("evidence".into(), json!({"items": self.evidence, "ms": self.ev_ms})));
                true
            }
            "answer" => {
                if let Some(t) = self.buf.find(TIMING_MARK) {
                    let body = self.buf[..t].to_string();
                    self.buf = self.buf[t..].to_string();
                    if !body.is_empty() {
                        self.answer.push_str(&body);
                        out.push(("token".into(), json!(body)));
                    }
                    self.state = "timing";
                    return true;
                }
                // 소요 표시의 앞부분일 수 있는 꼬리는 붙들어 둔다(글자 단위)
                let hold = TIMING_MARK.chars().count() + 2;
                let n = self.buf.chars().count();
                if n > hold {
                    let cut = self.buf.char_indices().nth(n - hold).map(|(i, _)| i).unwrap_or(0);
                    let send = self.buf[..cut].to_string();
                    self.buf = self.buf[cut..].to_string();
                    self.answer.push_str(&send);
                    out.push(("token".into(), json!(send)));
                    return true;
                }
                false
            }
            "timing" => {
                let p = match self.buf.find(PROMPT) {
                    Some(p) => p,
                    None => return false,
                };
                self.timing_text = self.buf[..p].to_string();
                self.buf = self.buf[p + PROMPT.len()..].to_string();
                self.state = "idle";
                out.push(("done".into(), self.done_info()));
                true
            }
            _ => false,
        }
    }

    // 근거 블록 → [{no, doc, snippet, full, path}]
    fn parse_evidence(&mut self, block: &str) {
        if let Some(i) = block.find("── 근거 ") {
            let tail = &block[i + "── 근거 ".len()..];
            let n: String = tail.chars().take_while(|c| c.is_ascii_digit()).collect();
            if let Some(p) = tail.find("건 (") {
                let ms: String = tail[p + "건 (".len()..].chars().take_while(|c| c.is_ascii_digit()).collect();
                self.ev_ms = ms.parse().unwrap_or(0);
            }
            let _ = n;
        }
        let mut items: Vec<Value> = vec![];
        for line in block.lines() {
            let line = line.trim_end_matches('\r');
            if let Some(js) = line.strip_prefix("  @ev ") {
                if let Ok(full) = serde_json::from_str::<Value>(js) {
                    for it in items.iter_mut() {
                        if it["no"] == full["no"] {
                            it["full"] = full.get("text").cloned().unwrap_or(json!(""));
                            it["path"] = full.get("path").cloned().unwrap_or(json!(""));
                        }
                    }
                }
                continue;
            }
            // "  [n] 문서이름"
            if line.starts_with("  [") && !line.starts_with("   ") {
                if let Some(close) = line.find("] ") {
                    let no = &line[3..close];
                    if let Ok(no) = no.parse::<i64>() {
                        items.push(json!({"no": no, "doc": line[close + 2..].trim(), "snippet": ""}));
                        continue;
                    }
                }
            }
            if line.starts_with("      ") {
                if let Some(last) = items.last_mut() {
                    let s = format!("{} {}", last["snippet"].as_str().unwrap_or(""), line.trim());
                    last["snippet"] = json!(s.trim());
                }
            }
        }
        self.evidence = items;
    }

    // 소요 블록 → 요약 정보
    fn done_info(&self) -> Value {
        let t = &self.timing_text;
        let grab = |pre: &str, stop: &str| -> Option<String> {
            let i = t.find(pre)? + pre.len();
            let rest = &t[i..];
            let j = rest.find(stop).unwrap_or(rest.len());
            Some(rest[..j].to_string())
        };
        let ttft = grab("첫 글자 ", "s").and_then(|s| s.parse::<f64>().ok());
        let total = grab("완료 ", "s").and_then(|s| s.parse::<f64>().ok());
        let search = grab("검색 ", "ms").and_then(|s| s.parse::<i64>().ok());
        let cited = grab("인용 근거: ", "\n").map(|s| s.trim().to_string()).unwrap_or_default();
        // "답변모델 qwen3-1.7b-q4 (정밀) · parent off"
        let (mut model, mut mode, mut parent) = (Value::Null, Value::Null, Value::Null);
        if let Some(line) = t.lines().find(|l| l.trim_start().starts_with("답변모델 ")) {
            let body = line.trim().trim_start_matches("답변모델 ");
            let (left, par) = body.split_once(" · parent ").unwrap_or((body, ""));
            let (m, md) = match left.split_once(" (") {
                Some((m, rest)) => (m.to_string(), Some(rest.trim_end_matches(')').to_string())),
                None => (left.to_string(), None),
            };
            model = json!(m);
            mode = md.map(|x| json!(x)).unwrap_or(Value::Null);
            parent = json!(par.trim());
        }
        let warns: Vec<String> = t.lines().filter(|l| l.contains('⚠')).map(|l| l.trim().to_string()).collect();
        json!({
            "answer": self.answer.trim(), "evidence": self.evidence, "search_ms": search,
            "ttft_s": ttft, "total_s": total, "cited": cited, "model": model, "model_mode": mode,
            "parent": parent, "warnings": warns, "parsed": self.saw_evidence,
        })
    }
}

// ── 워커 ───────────────────────────────────────────────

type Callback = Box<dyn FnOnce(Value) + Send>;

struct Inner {
    state: String,
    detail: String,
    backend: String,
    model: String,
    rerank_note: Option<String>,
    embed_model: String,
    rerank_model: String,
    ready_line: String,
    features: HashSet<String>,
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    parser: ChatParser,
    pending: Option<(String, Option<String>)>,
    current: Option<String>,
    t_state: Instant,
    t_last_ask: Instant,
    restarts: Vec<Instant>,
    blocked: Option<String>,
    stderr_tail: Vec<String>,
    want_running: bool,
    cmds: VecDeque<(String, Callback)>,
    cmd: Option<(String, Callback)>,
    cmd_buf: String,
    hold: Option<String>,
    gen: u64,
}

//------------------------------------------------------------------
// 워커 관리자
// -필드: cmd = 실행 명령 / env = 넣을 환경변수 / emit = 사건 받는 함수
//------------------------------------------------------------------
pub struct Worker {
    cmd: Vec<String>,
    env: Vec<(String, String)>,
    emit: Box<dyn Fn(WEvent) + Send + Sync>,
    no_stream: bool,
    start_timeout: Duration,
    answer_timeout: Duration,
    restart_max: usize,
    idle_unload: Duration,
    inner: Mutex<Inner>,
    job: Option<isize>,
    stop: std::sync::atomic::AtomicBool,
    utf8_stdin: bool,
}

impl Worker {
    #[allow(clippy::too_many_arguments)]
    pub fn new(cmd: Vec<String>, env: Vec<(String, String)>, emit: Box<dyn Fn(WEvent) + Send + Sync>, no_stream: bool,
               start_timeout_s: i64, answer_timeout_s: i64, restart_max: i64, idle_unload_min: i64) -> Arc<Worker> {
        // Rust 판 워커는 UTF-8 을, Python 판 simplerag.exe 는 cp949 를 받는다
        let utf8_stdin = cmd.first().map(|c| c.to_lowercase().contains("simplerag-rs")).unwrap_or(false);
        Arc::new(Worker {
            cmd,
            env,
            emit,
            no_stream,
            start_timeout: Duration::from_secs(start_timeout_s as u64),
            answer_timeout: Duration::from_secs(answer_timeout_s as u64),
            restart_max: restart_max.max(0) as usize,
            idle_unload: Duration::from_secs(idle_unload_min.max(0) as u64 * 60),
            inner: Mutex::new(Inner {
                state: STOPPED.into(), detail: "시작 전".into(), backend: String::new(), model: String::new(), rerank_note: None,
                embed_model: String::new(), rerank_model: String::new(), ready_line: String::new(), features: HashSet::new(),
                child: None, stdin: None, parser: ChatParser::new(), pending: None, current: None,
                t_state: Instant::now(), t_last_ask: Instant::now(), restarts: vec![], blocked: None, stderr_tail: vec![],
                want_running: false, cmds: VecDeque::new(), cmd: None, cmd_buf: String::new(), hold: None, gen: 0,
            }),
            job: crate::win::create_kill_job().map(|h| h.0 as isize),
            stop: std::sync::atomic::AtomicBool::new(false),
            utf8_stdin,
        })
    }

    //------------------------------------------------------------------
    // 관리 스레드 시작(0.2초마다 상태 확인)
    //------------------------------------------------------------------
    pub fn start(self: &Arc<Self>) {
        static STARTED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
        if STARTED.swap(true, std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        let me = self.clone();
        std::thread::Builder::new().name("rsb-worker".into()).spawn(move || {
            while !me.stop.load(std::sync::atomic::Ordering::SeqCst) {
                me.tick();
                std::thread::sleep(Duration::from_millis(200));
            }
        }).ok();
    }

    // 모델을 올려 두라
    pub fn ensure_started(&self) {
        let mut g = self.inner.lock().unwrap();
        g.want_running = true;
        g.blocked = None;
    }

    //------------------------------------------------------------------
    // 질문 맡기기 — 기다리는 질문은 하나뿐(새 질문이 오면 갈아 끼운다)
    //------------------------------------------------------------------
    pub fn ask(&self, question: &str, folder: Option<String>) {
        let state = {
            let mut g = self.inner.lock().unwrap();
            g.want_running = true;
            g.blocked = None;
            g.pending = Some((question.to_string(), folder));
            g.t_last_ask = Instant::now();
            g.state.clone()
        };
        crate::info!("worker", "질문 접수: {}", question.chars().take(20).collect::<String>());
        if state == STOPPED {
            (self.emit)(WEvent::State(STARTING.into(), "모델 준비 중 (첫 질문)".into()));
        }
    }

    // 인덱싱 명령 맡기기
    pub fn command(&self, line: String, cb: Callback) {
        self.inner.lock().unwrap().cmds.push_back((line, cb));
    }

    // up | starting | down
    pub fn availability(&self) -> &'static str {
        let g = self.inner.lock().unwrap();
        if g.child.is_some() && [READY, BUSY, INDEXING].contains(&g.state.as_str()) {
            "up"
        } else if g.state == STARTING {
            "starting"
        } else {
            "down"
        }
    }

    pub fn hold(&self, reason: Option<String>) {
        crate::info!("worker", "워커 띄우기 {}", match &reason { Some(r) => format!("막음: {}", r), None => "막음 풀림".into() });
        self.inner.lock().unwrap().hold = reason;
    }

    pub fn seconds_since_ask(&self) -> f64 {
        self.inner.lock().unwrap().t_last_ask.elapsed().as_secs_f64()
    }

    //------------------------------------------------------------------
    // 모델 내리기(인덱스 잠금도 풀린다)
    //------------------------------------------------------------------
    pub fn unload(&self, reason: &str) {
        {
            let mut g = self.inner.lock().unwrap();
            g.want_running = false;
            g.pending = None;
        }
        crate::info!("worker", "모델 내리기: {}", reason);
        self.terminate(true, reason);
        self.set_state(STOPPED, &format!("내려감 ({})", reason));
    }

    // 프로그램 끝
    pub fn shutdown(&self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        self.inner.lock().unwrap().want_running = false;
        self.terminate(true, "프로그램 종료");
    }

    //------------------------------------------------------------------
    // 트레이 툴팁용 자세한 상태
    //------------------------------------------------------------------
    pub fn status_text(&self) -> String {
        let g = self.inner.lock().unwrap();
        let name = match g.state.as_str() {
            STOPPED => "내려감", STARTING => "준비 중", READY => "준비됨", BUSY => "답변 중", _ => "인덱스 갱신 중",
        };
        let mut extra = if g.backend.is_empty() { g.detail.clone() } else { g.backend.clone() };
        if g.state == STARTING {
            extra = format!("{:.0}초째", g.t_state.elapsed().as_secs_f64());
        }
        if !g.model.is_empty() && (g.state == READY || g.state == BUSY) {
            let short = short_model(&g.model);
            extra = if extra.is_empty() { short } else { format!("{} · {}", short, extra) };
        }
        if extra.is_empty() { name.into() } else { format!("{} · {}", name, extra) }
    }

    pub fn state_label(&self) -> &'static str {
        match self.inner.lock().unwrap().state.as_str() {
            STOPPED => "준비 안 됨", STARTING => "준비 중", READY => "준비됨", BUSY => "답변 중", _ => "인덱싱 중",
        }
    }

    //------------------------------------------------------------------
    // 설정 창용 모델 정보
    //------------------------------------------------------------------
    pub fn info(&self) -> Value {
        let g = self.inner.lock().unwrap();
        let line = g.backend.trim();
        let (gen, rerank) = match line.split_once(" / 리랭킹 ") {
            Some((a, b)) => (a.trim_start_matches("생성 ").trim().to_string(), b.trim().to_string()),
            None => (line.trim_start_matches("생성 ").trim().to_string(), String::new()),
        };
        json!({"model": if g.model.is_empty() { String::new() } else { short_model(&g.model) }, "gen": gen, "rerank": rerank,
               "embed_model": g.embed_model, "rerank_model": g.rerank_model})
    }

    fn set_state(&self, state: &str, detail: &str) {
        {
            let mut g = self.inner.lock().unwrap();
            if g.state == state && g.detail == detail {
                return;
            }
            g.state = state.into();
            g.detail = detail.into();
            g.t_state = Instant::now();
        }
        crate::info!("worker", "상태: {} ({})", state, detail);
        (self.emit)(WEvent::State(state.into(), detail.into()));
    }

    //------------------------------------------------------------------
    // 워커 띄우기 — 콘솔 없이, 작업 개체에 묶어서
    //------------------------------------------------------------------
    fn launch(self: &Arc<Self>) -> bool {
        let mut args: Vec<String> = self.cmd[1..].to_vec();
        args.push("chat".into());
        if self.no_stream {
            args.push("--no-stream".into());
        }
        let cwd = std::path::Path::new(&self.cmd[0]).parent().map(|p| p.to_path_buf()).unwrap_or_default();
        crate::info!("worker", "워커 실행: {} {}", self.cmd[0], args.join(" "));
        let mut c = Command::new(&self.cmd[0]);
        c.args(&args).current_dir(cwd).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        for (k, v) in &self.env {
            c.env(k, v);
        }
        {
            use std::os::windows::process::CommandExt;
            c.creation_flags(0x0800_0000);
        }
        let mut child = match c.spawn() {
            Ok(ch) => ch,
            Err(e) => {
                (self.emit)(WEvent::Error(format!("SimpleRAG 를 실행하지 못했습니다: {}", e)));
                self.set_state(STOPPED, "실행 실패");
                self.inner.lock().unwrap().blocked = Some("실행 실패".into());
                return false;
            }
        };
        if let Some(j) = self.job {
            crate::win::assign_job(windows::Win32::Foundation::HANDLE(j as *mut _), &child);
        }
        let out = child.stdout.take();
        let err = child.stderr.take();
        let gen;
        {
            let mut g = self.inner.lock().unwrap();
            g.stdin = child.stdin.take();
            g.child = Some(child);
            g.parser = ChatParser::new();
            g.stderr_tail.clear();
            g.backend.clear();
            g.model.clear();
            g.rerank_note = None;
            g.embed_model.clear();
            g.rerank_model.clear();
            g.ready_line.clear();
            g.features.clear();
            g.gen += 1;
            gen = g.gen;
        }
        self.set_state(STARTING, "모델 적재 중");
        if let Some(o) = out {
            let me = self.clone();
            std::thread::spawn(move || me.read_stdout(o, gen));
        }
        if let Some(e) = err {
            let me = self.clone();
            std::thread::spawn(move || me.read_stderr(e));
        }
        true
    }

    //------------------------------------------------------------------
    // 표준 출력 읽기 — 시작 줄에서 모델·백엔드를 알아내고, 명령 결과·질문 답을 나눠 처리
    //------------------------------------------------------------------
    fn read_stdout(self: Arc<Self>, mut out: std::process::ChildStdout, gen: u64) {
        let mut buf = [0u8; 4096];
        let mut pend: Vec<u8> = vec![];
        loop {
            let n = match out.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            pend.extend_from_slice(&buf[..n]);
            // 덜 끝난 UTF-8 글자는 다음 조각에 붙인다
            let valid = match std::str::from_utf8(&pend) {
                Ok(_) => pend.len(),
                Err(e) if e.error_len().is_none() => e.valid_up_to(),
                Err(_) => pend.len(),
            };
            let text = String::from_utf8_lossy(&pend[..valid]).replace('\r', "");
            pend.drain(..valid);
            if text.is_empty() {
                continue;
            }
            if self.inner.lock().unwrap().gen != gen {
                break;
            }
            let starting = self.inner.lock().unwrap().state == STARTING;
            if starting {
                let mut g = self.inner.lock().unwrap();
                for ln in text.lines() {
                    if ln.starts_with("생성 ") {
                        g.backend = ln.trim().to_string();
                        if let Some(n) = g.rerank_note.clone() {
                            if g.backend.contains(RERANK_LOADING) {
                                g.backend = g.backend.replace(RERANK_LOADING, &n);
                            }
                        }
                    } else if ln.starts_with("준비 완료") {
                        g.model = ln.split_whitespace().find(|w| w.ends_with(".gguf")).unwrap_or("").to_string();
                        g.ready_line = ln.trim().to_string();
                    } else if ln.starts_with("모델:") {
                        let body = ln.split_once(':').map(|x| x.1).unwrap_or("");
                        for part in body.split(" / ") {
                            let p = part.trim();
                            if let Some(x) = p.strip_prefix("임베딩 ") {
                                g.embed_model = x.trim().into();
                            } else if let Some(x) = p.strip_prefix("리랭커 ") {
                                g.rerank_model = x.trim().into();
                            }
                        }
                    } else if ln.starts_with("기능:") {
                        g.features = ln.split_once(':').map(|x| x.1).unwrap_or("").split_whitespace().map(|s| s.to_string()).collect();
                    }
                }
            }
            let mut text = text;
            let in_cmd = self.inner.lock().unwrap().cmd.is_some();
            if in_cmd {
                let done = {
                    let mut g = self.inner.lock().unwrap();
                    g.cmd_buf.push_str(&text);
                    match g.cmd_buf.find(PROMPT) {
                        Some(i) => {
                            let outp = g.cmd_buf[..i].to_string();
                            let rest = g.cmd_buf[i + PROMPT.len()..].to_string();
                            g.cmd_buf.clear();
                            Some((outp, rest))
                        }
                        None => None,
                    }
                };
                match done {
                    None => continue,
                    Some((outp, rest)) => {
                        self.finish_cmd(&outp);
                        if rest.is_empty() {
                            continue;
                        }
                        text = rest;
                    }
                }
            }
            let evs = self.inner.lock().unwrap().parser.feed(&text);
            for (kind, v) in evs {
                self.on_parsed(&kind, v);
            }
        }
    }

    fn on_parsed(&self, kind: &str, v: Value) {
        match kind {
            "ready" => {
                let (line, backend, feats) = {
                    let g = self.inner.lock().unwrap();
                    (g.ready_line.clone(), g.backend.clone(), g.features.iter().cloned().collect::<Vec<_>>().join(" "))
                };
                if !line.is_empty() {
                    crate::info!("worker", "워커 {}", line);
                }
                if !backend.is_empty() {
                    crate::info!("worker", "워커 {}", backend);
                }
                crate::info!("worker", "워커 기능: {}", if feats.is_empty() { "(없음 — 옛 simplerag)".into() } else { feats });
                self.set_state(READY, if backend.is_empty() { "준비됨" } else { &backend });
            }
            "done" => {
                self.inner.lock().unwrap().current = None;
                (self.emit)(WEvent::Done(v));
                let b = self.inner.lock().unwrap().backend.clone();
                self.set_state(READY, if b.is_empty() { "준비됨" } else { &b });
            }
            "evidence" => (self.emit)(WEvent::Evidence(v["items"].as_array().cloned().unwrap_or_default(), v["ms"].as_i64().unwrap_or(0))),
            "token" => (self.emit)(WEvent::Token(v.as_str().unwrap_or("").to_string())),
            "raw" => (self.emit)(WEvent::Raw(v.as_str().unwrap_or("").to_string())),
            _ => {}
        }
    }

    //------------------------------------------------------------------
    // 표준 오류 읽기 — worker.log 에 남기고 잠금·리랭커 알림을 본다
    //------------------------------------------------------------------
    fn read_stderr(self: Arc<Self>, err: std::process::ChildStderr) {
        let mut r = std::io::BufReader::new(err);
        let mut line = vec![];
        loop {
            line.clear();
            match std::io::BufRead::read_until(&mut r, b'\n', &mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
            let s = String::from_utf8_lossy(&line).trim_end().to_string();
            if s.is_empty() {
                continue;
            }
            crate::log::worker(&s);
            let mut g = self.inner.lock().unwrap();
            g.stderr_tail.push(s.clone());
            let n = g.stderr_tail.len();
            if n > 50 {
                g.stderr_tail.drain(..n - 50);
            }
            if s.contains(LOCK_HINT) {
                g.blocked = Some("SimpleRAG chat/index 가 실행 중입니다 — 끝낸 뒤 다시 질문하세요".into());
            }
            let note = if s.starts_with(RERANK_DONE) {
                Some("켬")
            } else if s.starts_with(RERANK_FAIL) {
                Some("적재 실패(리랭킹 없이 답함)")
            } else {
                None
            };
            if let Some(n) = note {
                g.rerank_note = Some(n.into());
                if g.backend.contains(RERANK_LOADING) {
                    g.backend = g.backend.replace(RERANK_LOADING, n);
                }
            }
        }
    }

    // 한 줄 보내기(인코딩 맞춰)
    fn write_line(&self, g: &mut Inner, line: &str) -> std::io::Result<()> {
        let stdin = g.stdin.as_mut().ok_or_else(|| std::io::Error::other("stdin 없음"))?;
        let bytes: Vec<u8> = if self.utf8_stdin {
            format!("{}\n", line).into_bytes()
        } else {
            encoding_rs::EUC_KR.encode(&format!("{}\n", line)).0.into_owned()
        };
        stdin.write_all(&bytes)?;
        stdin.flush()
    }

    // 기다리던 질문 보내기
    fn send_pending(&self) -> bool {
        let line;
        let q;
        {
            let mut g = self.inner.lock().unwrap();
            let (qq, folder) = match g.pending.take() {
                Some(p) => p,
                None => return false,
            };
            if g.child.is_none() {
                return false;
            }
            q = qq;
            g.current = Some(q.clone());
            g.parser.begin_question();
            let use_ask = folder.is_some() && g.features.contains("ask-folder");
            line = if use_ask { format!("/ask {}", ascii_json(&json!({"q": q, "folder": folder}))) } else { q.clone() };
            if let Err(e) = self.write_line(&mut g, &line) {
                crate::warn!("worker", "질문 전송 실패: {}", e);
                return false;
            }
        }
        self.set_state(BUSY, "검색 중");
        true
    }

    // 인덱싱 명령 하나 보내기
    fn send_cmd(&self) -> bool {
        let label;
        let fail;
        {
            let mut g = self.inner.lock().unwrap();
            if g.cmds.is_empty() || g.child.is_none() || g.cmd.is_some() {
                return false;
            }
            let (line, cb) = g.cmds.pop_front().unwrap();
            g.cmd_buf.clear();
            let r = self.write_line(&mut g, &line);
            label = line.split(' ').next().unwrap_or("").to_string();
            match r {
                Ok(_) => {
                    g.cmd = Some((line, cb));
                    fail = None;
                }
                Err(e) => {
                    crate::warn!("worker", "인덱싱 명령 전송 실패: {}", e);
                    fail = Some(cb);
                }
            }
        }
        if let Some(cb) = fail {
            cb(json!({"ok": false, "stopped": true, "why": "워커에 보내지 못함"}));
            return false;
        }
        self.set_state(INDEXING, &label);
        true
    }

    // 명령 결과 줄 해석
    fn finish_cmd(&self, out: &str) {
        let cmd = self.inner.lock().unwrap().cmd.take();
        let (_, cb) = match cmd {
            Some(c) => c,
            None => return,
        };
        let mut res: Option<Value> = None;
        for ln in out.lines() {
            if let Some(js) = ln.strip_prefix(INDEX_PREFIX) {
                res = serde_json::from_str(js).ok();
            }
        }
        let res = res.unwrap_or_else(|| json!({"ok": false, "unknown": true,
            "why": "워커가 인덱싱 명령을 모릅니다 — simplerag-rs.exe 를 새로 빌드하세요",
            "raw": out.trim().chars().take(200).collect::<String>()}));
        let b = self.inner.lock().unwrap().backend.clone();
        self.set_state(READY, if b.is_empty() { "준비됨" } else { &b });
        cb(res);
    }

    // 기다리던 명령 모두 실패 처리
    fn fail_cmds(&self, why: &str) {
        let pending: Vec<Callback> = {
            let mut g = self.inner.lock().unwrap();
            let mut v: Vec<Callback> = vec![];
            if let Some((_, cb)) = g.cmd.take() {
                v.push(cb);
            }
            while let Some((_, cb)) = g.cmds.pop_front() {
                v.push(cb);
            }
            g.cmd_buf.clear();
            v
        };
        for cb in pending {
            cb(json!({"ok": false, "stopped": true, "why": why}));
        }
    }

    //------------------------------------------------------------------
    // 워커 끝내기 — 점잖게(exit) 기다렸다가 안 끝나면 죽인다
    //------------------------------------------------------------------
    fn terminate(&self, graceful: bool, reason: &str) {
        let (child, mut stdin) = {
            let mut g = self.inner.lock().unwrap();
            g.current = None;
            g.gen += 1;
            (g.child.take(), g.stdin.take())
        };
        self.fail_cmds(&format!("워커 종료({})", reason));
        let mut child = match child {
            Some(c) => c,
            None => return,
        };
        if graceful {
            if let Some(s) = stdin.as_mut() {
                let _ = s.write_all(b"exit\n");
                let _ = s.flush();
            }
            drop(stdin);
            let t0 = Instant::now();
            while t0.elapsed() < Duration::from_secs(5) {
                if let Ok(Some(_)) = child.try_wait() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
        if let Ok(None) = child.try_wait() {
            let _ = child.kill();
        }
        let code = child.wait().ok().and_then(|s| s.code());
        crate::info!("worker", "워커 종료({}) 코드={:?}", reason, code);
    }

    // 워커가 스스로 끝났을 때
    fn handle_exit(&self, code: i32) {
        let (current, tail, blocked) = {
            let mut g = self.inner.lock().unwrap();
            let cur = g.current.take();
            g.child = None;
            g.stdin = None;
            g.gen += 1;
            if cur.is_some() {
                g.pending = None;
            }
            (cur, g.stderr_tail.clone(), g.blocked.clone())
        };
        self.fail_cmds(&format!("워커가 종료됨(code={})", code));
        crate::warn!("worker", "워커가 종료됨 code={} (답변 중이던 질문: {:?})", code, current);
        for l in tail.iter().rev().take(10).rev() {
            crate::warn!("worker", "  stderr: {}", l);
        }
        if code == 2 {
            self.inner.lock().unwrap().blocked = Some("설정 오류 — config.yaml 을 확인하세요".into());
            let msg = tail.iter().rev().find(|l| l.contains("설정 오류")).cloned().unwrap_or_else(|| "config.yaml 확인".into());
            (self.emit)(WEvent::Error(format!("설정 오류로 SimpleRAG 가 멈췄습니다: {}", msg)));
        } else if let Some(b) = blocked {
            (self.emit)(WEvent::Error(b));
        } else if current.is_some() {
            (self.emit)(WEvent::Error("SimpleRAG 가 종료되었습니다 — 다시 올립니다".into()));
        }
        self.set_state(STOPPED, &format!("종료됨(code={})", code));
    }

    //------------------------------------------------------------------
    // 0.2초마다 — 죽음·시간 초과·대기 질문·명령·유휴 내리기
    //------------------------------------------------------------------
    fn tick(self: &Arc<Self>) {
        let exited = {
            let mut g = self.inner.lock().unwrap();
            match g.child.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(st))) => Some(st.code().unwrap_or(-1)),
                _ => None,
            }
        };
        if let Some(code) = exited {
            self.handle_exit(code);
            return;
        }
        let (has_child, state, want, pending, blocked, t_state, t_last, hold, has_cmd) = {
            let g = self.inner.lock().unwrap();
            (g.child.is_some(), g.state.clone(), g.want_running, g.pending.is_some(), g.blocked.clone(),
             g.t_state, g.t_last_ask, g.hold.clone(), !g.cmds.is_empty() && g.cmd.is_none())
        };
        if !has_child && want && state == STOPPED {
            if blocked.is_some() {
                return;
            }
            if hold.is_some() {
                if pending {
                    self.set_state(STOPPED, "인덱스 갱신 중 — 끝나면 답합니다");
                }
                return;
            }
            let can = {
                let mut g = self.inner.lock().unwrap();
                g.restarts.retain(|t| t.elapsed() < Duration::from_secs(RESTART_WINDOW_S));
                g.restarts.len() < self.restart_max
            };
            if !can {
                self.set_state(STOPPED, "재시작 한도 초과 — 트레이에서 다시 올리세요");
                self.inner.lock().unwrap().blocked = Some("재시작 한도를 넘었습니다".into());
                (self.emit)(WEvent::Error("SimpleRAG 가 반복해서 종료됩니다 — 로그를 확인하세요".into()));
                return;
            }
            self.inner.lock().unwrap().restarts.push(Instant::now());
            self.launch();
            return;
        }
        if state == STARTING && t_state.elapsed() > self.start_timeout {
            (self.emit)(WEvent::Error("모델을 올리지 못했습니다(시간 초과) — 다시 시도합니다".into()));
            self.terminate(false, "준비 시간 초과");
            self.set_state(STOPPED, "준비 시간 초과");
            return;
        }
        if state == BUSY && t_state.elapsed() > self.answer_timeout {
            (self.emit)(WEvent::Error("답변이 멈췄습니다 — 모델을 다시 올립니다".into()));
            self.terminate(false, "답변 시간 초과");
            self.set_state(STOPPED, "답변 시간 초과");
            return;
        }
        if state == INDEXING && t_state.elapsed() > Duration::from_secs(CMD_TIMEOUT_S) {
            (self.emit)(WEvent::Note("index".into(), "인덱싱 명령이 멈춰 모델을 다시 올립니다".into()));
            self.terminate(false, "인덱싱 시간 초과");
            self.set_state(STOPPED, "인덱싱 시간 초과");
            return;
        }
        if state == READY && pending {
            self.send_pending();
            return;
        }
        if state == READY && has_cmd {
            self.send_cmd();
            return;
        }
        if state == READY && !self.idle_unload.is_zero() && t_last.elapsed() > self.idle_unload {
            self.unload(&format!("{}분 유휴", self.idle_unload.as_secs() / 60));
        }
    }
}

// 모델 파일 이름 → 짧은 이름("Qwen3-1.7B")
fn short_model(m: &str) -> String {
    m.split("-Q4").next().unwrap_or(m).split(".gguf").next().unwrap_or(m).to_string()
}

//------------------------------------------------------------------
// ASCII JSON(파이썬 json.dumps ensure_ascii=True, 기본 구분자)
//------------------------------------------------------------------
pub fn ascii_json(v: &Value) -> String {
    let s = serde_json::to_string(v).unwrap_or_default();
    let mut out = String::new();
    // serde 는 ", " 대신 "," 를 쓰지만 워커는 JSON 으로 읽으므로 상관없다. 비 ASCII 만 \u 로 바꾼다
    for ch in s.chars() {
        if (ch as u32) < 0x80 {
            out.push(ch);
        } else {
            let mut b = [0u16; 2];
            for u in ch.encode_utf16(&mut b) {
                out.push_str(&format!("\\u{:04x}", u));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parse_flow() {
        let mut p = ChatParser::new();
        let ev = p.feed("모델 적재 중...\n준비 완료\n질문> ");
        assert_eq!(ev[0].0, "ready");
        p.begin_question();
        let mut all = p.feed("\n── 근거 1건 (12ms) ───\n  [1] a.pdf\n      본문\n  @ev {\"no\": 1, \"path\": \"D:\\\\a.pdf\", \"text\": \"전체\"}\n\n── 답변 (AI 요약 — 위 근거로 확인하세요) ──\n답입니다 [1]");
        all.extend(p.feed("\n\n── 소요 ───\n  검색 12ms (임베딩 1)\n  첫 글자 1.20s / 완료 2.50s\n  답변모델 qwen3-1.7b-q4 (정밀) · parent off\n  인용 근거: [1]\n\n질문> "));
        let kinds: Vec<&str> = all.iter().map(|e| e.0.as_str()).collect();
        assert_eq!(kinds.first(), Some(&"evidence"));
        assert_eq!(kinds.last(), Some(&"done"));
        let done = &all.last().unwrap().1;
        assert_eq!(done["answer"], "답입니다 [1]");
        assert_eq!(done["model"], "qwen3-1.7b-q4");
        assert_eq!(done["parent"], "off");
        assert_eq!(done["search_ms"], 12);
        assert_eq!(all[0].1["items"][0]["full"], "전체");
    }
}
