//------------------------------------------------------------------
// RAGSearchBox-rs — 트레이 상주 앱 진입점 (Python main.py · app.py)
//=> 탐색기에서 지정 폴더를 열면 오른쪽에 대화 패널이 붙고, 질문은 상주 워커(simplerag-rs chat)가 답한다.
//   구성: 트레이(자기 스레드) · 탐색기 감시(자기 스레드) · 워커 관리(자기 스레드) · 폴더 감시(폴더마다 스레드)
//         · 패널/설정 창(메인 스레드, WebView2). 다른 스레드의 일은 모두 사용자 이벤트로 메인에 넘어온다.
//------------------------------------------------------------------
#![windows_subsystem = "windows"]

mod explorer;
mod history;
mod jobs;
mod log;
mod monitor;
mod panel;
mod scope;
mod settings;
mod synonyms;
mod tray;
mod watcher;
mod win;
mod worker;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
use tao::platform::windows::{IconExtWindows, WindowExtWindows};
use tao::window::{Icon, WindowBuilder};
use wry::{WebContext, WebViewBuilder};

use jobs::{IndexJobs, JobMsg};
use monitor::{MEvent, Monitor};
use panel::Panel;
use scope::Scope;
use settings::Settings;
use tray::Tray;
use watcher::{Batch, FolderWatcher};
use worker::{WEvent, Worker};

const MUTEX_NAME: &str = "Local\\RAGSearchBox-rs";
const PUMP: Duration = Duration::from_millis(50);
const TOOLTIP_EVERY: Duration = Duration::from_secs(2);
const INFO_EVERY: Duration = Duration::from_secs(1);
const SCOPE_WARN: &str = "범위 폴더 미설정 — RAGSearchBox.ini 의 [Scope] Folders 를 지정하세요";
const PANEL_HTML: &str = include_str!("../ui/panel.html");
const INFO_HTML: &str = include_str!("../ui/info.html");
const MOAI_PNG: &[u8] = include_bytes!("../moai.png");

//------------------------------------------------------------------
// 다른 스레드가 메인에 넘기는 일
//------------------------------------------------------------------
enum UserEvent {
    Worker(WEvent),
    Monitor(MEvent),
    Tray(String),
    Watch(Batch),
    Job(JobMsg),
    AutoIndex(String, String),
    PanelIpc(String),
    InfoIpc(String),
}

type Proxy = Arc<Mutex<EventLoopProxy<UserEvent>>>;

fn post(p: &Proxy, e: UserEvent) {
    let _ = p.lock().unwrap().send_event(e);
}

//------------------------------------------------------------------
// 설정 창 상태
//------------------------------------------------------------------
struct InfoWin {
    window: tao::window::Window,
    webview: wry::WebView,
    visible: bool,
    ready: bool,
    action_msg: String,
    folder_msg: String,
    // 동의어 칸 마지막 결과 글 · 후보 만들기 뒤 스레드가 남긴 소식(다음 갱신 때 syn_msg 로 옮긴다) · 도는 중인가
    syn_msg: String,
    syn_done: Arc<Mutex<Option<String>>>,
    syn_running: Arc<std::sync::atomic::AtomicBool>,
}

//------------------------------------------------------------------
// 앱 — 모든 구성요소
//------------------------------------------------------------------
struct App {
    s: Settings,
    proxy: Proxy,
    scope: Arc<Mutex<Scope>>,
    worker: Option<Arc<Worker>>,
    worker_error: Option<String>,
    cmd: Vec<String>,
    jobs: Option<IndexJobs>,
    watcher: Option<Arc<FolderWatcher>>,
    tray: Tray,
    monitor: Monitor,
    panel: Panel,
    info: InfoWin,
    history: Vec<Value>,
    history_path: String,
    answer_hwnd: isize,
    state_cache: (Option<std::time::SystemTime>, Vec<String>),
    last_tooltip: String,
    t_tooltip: Instant,
    t_info: Instant,
    quitting: bool,
    selftest_done: bool,
}

//------------------------------------------------------------------
// 명령 출력에서 결과 줄("@index {json}") 꺼내기 (Python parse_result_line 과 같다) — 없으면 옛 simplerag
//------------------------------------------------------------------
fn parse_result_line(out: &str) -> Value {
    let mut res = None;
    for ln in out.lines() {
        if let Some(js) = ln.strip_prefix("@index ") {
            if let Ok(v) = serde_json::from_str::<Value>(js) {
                res = Some(v);
            }
        }
    }
    res.unwrap_or_else(|| json!({"ok": false, "unknown": true, "why": out.trim().lines().last().unwrap_or("")}))
}

//------------------------------------------------------------------
// 후보 만들기 결과 → 설정 창 소식 글 (Python synonym_result_msg 와 같은 글)
//------------------------------------------------------------------
fn synonym_result_msg(res: &Value) -> String {
    if res["ok"].as_bool().unwrap_or(false) {
        let n = |k: &str| res.get(k).map(|v| v.to_string()).unwrap_or_else(|| "?".into());
        return format!("후보를 만들었습니다: 질문 {}개 → 후보 낱말 {}개 ({})", n("n_questions"), n("n_words"),
                       if res["llm"].as_bool().unwrap_or(false) { "1.7B 고르기" } else { "e5 순서" });
    }
    if res["stopped"].as_bool().unwrap_or(false) {
        return "워커가 내려가 중단됐습니다 — 다시 누르세요".into();
    }
    if res["unknown"].as_bool().unwrap_or(false) {
        return "SimpleRAG 가 이 명령을 모릅니다 — simplerag 를 새로 빌드하세요".into();
    }
    format!("후보를 만들지 못했습니다: {}", res["why"].as_str().filter(|s| !s.is_empty()).unwrap_or("알 수 없는 이유"))
}

// 글 한 줄로(공백 접기 · 앞 '/' 떼기 · 종료 낱말 보호) — query_filter.one_line
fn one_line(text: &str) -> Option<String> {
    let s = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let s = s.trim_start_matches('/').trim().to_string();
    if s.is_empty() {
        return None;
    }
    if ["exit", "quit", "종료", "/exit", "/quit"].contains(&s.to_lowercase().as_str()) {
        return Some(format!("{} ?", s));
    }
    Some(s)
}

fn font_px(pt: i64) -> String {
    format!("{:.1}", pt as f64 * 96.0 / 72.0)
}

impl App {
    //------------------------------------------------------------------
    // 시작 — 트레이 알림, 워커 예열(boot), 자동 시작 경로 고치기, 감시·폴더 감시 시작
    //------------------------------------------------------------------
    fn start(&mut self) {
        for w in self.s.warnings.clone() {
            crate::warn!("app", "설정: {}", w);
        }
        if !self.s.warnings.is_empty() {
            self.tray.notify("RAGSearchBox-rs 설정 확인", &self.s.warnings.iter().take(3).cloned().collect::<Vec<_>>().join("\n"));
        }
        if let Some(e) = &self.worker_error {
            self.tray.notify("RAGSearchBox-rs", e);
        }
        if self.s.searchbox_trigger {
            crate::warn!("app", "[Trigger] SearchBox=1(검색창 ? 질문)은 Rust 판에서 지원하지 않는다 — 폴더 패널만 동작한다");
        }
        let usable = self.scope.lock().unwrap().usable();
        if !usable {
            crate::warn!("app", "{}", SCOPE_WARN);
            self.tray.notify("RAGSearchBox-rs", SCOPE_WARN);
        } else if let Some(w) = &self.worker {
            w.start();
            if self.s.start_mode == "boot" {
                crate::info!("app", "StartMode=boot — 모델을 미리 올린다");
                w.ensure_started();
            }
        }
        if let Some(fixed) = win::autorun_fix_path() {
            crate::info!("app", "자동 시작 경로를 고쳤다: {}", fixed);
        }
        if self.jobs.as_ref().map(|j| j.enabled).unwrap_or(false) {
            self.start_watcher();
        }
        self.update_tooltip();
        crate::info!("app", "시작 완료 (범위 폴더 {}개)", self.scope.lock().unwrap().active.len());
    }

    // 50ms 마다 — 패널 따라가기, 자동 인덱싱, 툴팁·설정 창 갱신
    fn pump(&mut self) {
        self.panel.follow(&mut self.s);
        if let Some(j) = self.jobs.as_mut() {
            j.tick();
        }
        if self.t_tooltip.elapsed() >= TOOLTIP_EVERY {
            self.update_tooltip();
        }
        if self.info.visible && self.t_info.elapsed() >= INFO_EVERY {
            self.refresh_info();
        }
    }

    fn handle(&mut self, ev: UserEvent) {
        match ev {
            UserEvent::Worker(w) => self.on_worker(w),
            UserEvent::Monitor(MEvent::Folder(h, f)) => self.on_folder(h, f),
            UserEvent::Monitor(MEvent::Note(k, m)) => self.on_note(&k, &m),
            UserEvent::Tray(name) => self.on_tray(&name),
            UserEvent::Watch(b) => {
                if let Some(j) = self.jobs.as_mut() {
                    j.on_batch(&b);
                }
            }
            UserEvent::Job(m) => {
                if let Some(j) = self.jobs.as_mut() {
                    j.on_msg(m);
                }
            }
            UserEvent::AutoIndex(k, m) => self.on_autoindex(&k, &m),
            UserEvent::PanelIpc(s) => self.on_panel_ipc(&s),
            UserEvent::InfoIpc(s) => self.on_info_ipc(&s),
        }
    }

    //------------------------------------------------------------------
    // 워커 사건 → 패널
    //------------------------------------------------------------------
    fn on_worker(&mut self, ev: WEvent) {
        match ev {
            WEvent::State(state, detail) => {
                if state == worker::STARTING {
                    self.panel.set_state("모델 준비 중 (첫 질문)");
                } else if state == worker::STOPPED && detail.contains("인덱스") {
                    self.panel.set_state(&detail);
                } else if state == worker::BUSY {
                    self.panel.set_state("검색 중");
                }
                self.update_tooltip();
            }
            WEvent::Evidence(items, _ms) => self.panel.call("evidence", Value::Array(items)),
            WEvent::Token(t) => self.panel.call("token", json!(t)),
            WEvent::Done(info) => self.panel.call("finish", info),
            WEvent::Raw(t) => self.panel.call("raw", json!(t)),
            WEvent::Error(m) => self.on_error(&m),
            WEvent::Note(k, m) => self.on_note(&k, &m),
        }
    }

    fn on_error(&mut self, msg: &str) {
        crate::warn!("app", "오류: {}", msg);
        if self.panel.visible {
            self.panel.show_error(msg);
        } else {
            self.tray.notify("RAGSearchBox-rs", msg);
        }
    }

    fn on_note(&mut self, kind: &str, msg: &str) {
        match kind {
            "evidence" => {
                crate::info!("app", "근거 목록 표시: {}", msg.chars().take(60).collect::<String>());
                self.panel.notice("근거 파일을 탐색기에 띄웠습니다");
            }
            "evidence_fail" => {
                crate::info!("app", "근거 목록 실패: {}", msg);
                self.panel.notice(msg);
            }
            _ => {
                crate::warn!("app", "감시 알림({}): {}", kind, msg);
                self.tray.notify("RAGSearchBox-rs", msg);
            }
        }
    }

    //------------------------------------------------------------------
    // 탐색기 폴더가 바뀌었다 — 범위 안이면 패널을 붙이고, 밖이면 떼어 낸다
    //------------------------------------------------------------------
    fn on_folder(&mut self, hwnd: isize, folder: Option<String>) {
        if !self.s.panel_enabled {
            return;
        }
        match folder {
            Some(f) => {
                self.panel.show_for(hwnd, &f, &self.s);
                if self.panel.visible && self.panel.target == hwnd {
                    self.monitor.watch_window(Some(hwnd));
                    self.answer_hwnd = hwnd;
                    self.refresh_scope();
                    // 자체 시험(RSB_SELFTEST_ASK) — 패널이 처음 뜨면 그 질문을 입력 칸에 넣고 보낸다
                    if let Ok(q) = std::env::var("RSB_SELFTEST_ASK") {
                        if !q.is_empty() && self.panel.visible && !self.selftest_done {
                            self.selftest_done = true;
                            crate::info!("app", "자체 시험 질문: {}", q);
                            self.panel.js(format!("document.getElementById('input').value = {}; doSend();", json!(q)));
                        }
                    }
                    if let Some(w) = &self.worker {
                        if self.s.start_mode != "lazy" {
                            w.ensure_started();
                        }
                    }
                }
            }
            None => {
                self.panel.forget_closed(hwnd);
                if self.panel.visible && self.panel.target == hwnd {
                    self.panel.hide(true);
                    self.monitor.watch_window(None);
                }
            }
        }
    }

    // 질문 범위 폴더 — root 모드면 그 폴더가 속한 지정 폴더
    fn search_folder(&self, folder: Option<&str>) -> Option<String> {
        let f = folder?;
        if self.panel.scope_mode == "root" {
            let (ok, root) = self.scope.lock().unwrap().contains(f);
            return Some(if ok { root } else { f.to_string() });
        }
        Some(f.to_string())
    }

    //------------------------------------------------------------------
    // 워커의 데이터 폴더(인덱스 상태 파일 · 동의어 사전·후보) — SIMPLERAG_HOME → 워커 exe 옆
    //------------------------------------------------------------------
    fn data_home(&self) -> Option<String> {
        match std::env::var("SIMPLERAG_HOME") {
            Ok(h) if !h.is_empty() => Some(h),
            _ => Some(std::path::Path::new(self.cmd.last()?).parent()?.to_string_lossy().into_owned()),
        }
    }

    //------------------------------------------------------------------
    // 인덱스 상태 파일의 문서 키들(소문자) — mtime 이 같으면 다시 읽지 않는다
    //------------------------------------------------------------------
    fn state_keys(&mut self) -> Option<Vec<String>> {
        let home = self.data_home()?;
        let path = std::path::Path::new(&home).join("index_state.json");
        let mtime = std::fs::metadata(&path).ok()?.modified().ok()?;
        if self.state_cache.0 == Some(mtime) {
            return Some(self.state_cache.1.clone());
        }
        let v: Value = serde_json::from_slice(&std::fs::read(&path).ok()?).ok()?;
        let keys: Vec<String> = v.get("docs")?.as_object()?.keys().map(|k| k.to_lowercase()).collect();
        self.state_cache = (Some(mtime), keys.clone());
        Some(keys)
    }

    // 범위 설명 — "범위: 이름 · 문서 N건"
    fn scope_desc(&mut self) -> String {
        let folder = match self.search_folder(self.panel.folder.clone().as_deref()) {
            Some(f) => f,
            None => return String::new(),
        };
        let name = folder.trim_end_matches(['\\', '/']).rsplit(['\\', '/']).next().unwrap_or(&folder).to_string();
        let name = if name.is_empty() { folder.clone() } else { name };
        let n = self.state_keys().map(|keys| {
            let base = format!("{}\\", scope::norm(&folder).trim_end_matches('\\'));
            keys.iter().filter(|k| k.starts_with(&base)).count()
        });
        format!("범위: {} · 문서 {}건", name, n.map(|x| x.to_string()).unwrap_or("?".into()))
    }

    fn refresh_scope(&mut self) {
        let d = self.scope_desc();
        self.panel.call("setScope", json!(d));
    }

    fn history_list(&self) -> Value {
        Value::Array(self.history.iter().map(|c| {
            let mut c = c.clone();
            c["label"] = json!(history::menu_label(&c));
            c
        }).collect())
    }

    //------------------------------------------------------------------
    // 패널 화면이 보낸 메시지
    //------------------------------------------------------------------
    fn on_panel_ipc(&mut self, raw: &str) {
        let m: Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(_) => return,
        };
        match m["type"].as_str().unwrap_or("") {
            "ready" => {
                self.panel.on_ready();
                let h = self.history_list();
                self.panel.call("setHistory", h);
                let mode = self.panel.scope_mode.clone();
                self.panel.call("setScopeMode", json!(mode));
                if let Some(f) = self.panel.folder.clone() {
                    self.panel.call("setFolder", json!(f));
                }
                self.refresh_scope();
            }
            "ask" => {
                let q = match one_line(m["text"].as_str().unwrap_or("")) {
                    Some(q) => q,
                    None => return,
                };
                let folder = self.search_folder(self.panel.folder.clone().as_deref());
                crate::info!("app", "패널 질문: {:?} (범위 {:?})", q.chars().take(40).collect::<String>(), folder);
                match &self.worker {
                    Some(w) => w.ask(&q, folder),
                    None => {
                        let e = self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into());
                        self.panel.show_error(&e);
                    }
                }
            }
            "save" => {
                let mut conv = m["conv"].clone();
                history::touch(&mut self.history, &mut conv);
                if !history::save(&self.history_path, &self.history) {
                    crate::warn!("panel", "대화 기록을 저장하지 못했다: {}", self.history_path);
                }
                let h = self.history_list();
                self.panel.call("setHistory", h);
            }
            "clear_history" => {
                self.history.clear();
                history::save(&self.history_path, &self.history);
                self.panel.call("setHistory", json!([]));
                crate::info!("panel", "대화 기록을 모두 지웠다");
            }
            "open_file" => {
                let p = m["path"].as_str().unwrap_or("");
                if let Err(e) = win::open_path(p) {
                    self.panel.set_state(&format!("파일을 열지 못했습니다: {}", e));
                    crate::warn!("panel", "출처 파일을 열지 못했다: {} ({})", p, e);
                }
            }
            "show_files" => {
                let docs: Vec<String> = m["docs"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).filter(|s| !s.is_empty()).collect()).unwrap_or_default();
                if self.answer_hwnd == 0 || docs.is_empty() {
                    self.panel.notice("근거 파일을 알 수 없습니다");
                    return;
                }
                self.monitor.show_evidence(self.answer_hwnd, docs);
            }
            "scope" => {
                let mode = m["mode"].as_str().unwrap_or("folder").to_string();
                crate::info!("panel", "검색 범위: {}", mode);
                self.panel.scope_mode = mode.clone();
                if let Err(e) = settings::save_ini_value(&self.s.ini_path, "Panel", "SearchScope", &mode) {
                    crate::warn!("app", "검색 범위를 설정에 저장하지 못했다: {}", e);
                }
                self.refresh_scope();
            }
            "close" => self.panel.close_by_user(),
            _ => {}
        }
    }

    //------------------------------------------------------------------
    // 트레이 메뉴
    //------------------------------------------------------------------
    fn on_tray(&mut self, name: &str) {
        crate::info!("app", "트레이: {}", name);
        match name {
            "open" => self.open_panel(),
            "index_now" | "autoindex" => self.on_tray_autoindex(name),
            "unload" => {
                if let Some(w) = &self.worker {
                    w.unload("트레이 메뉴");
                    self.tray.notify("RAGSearchBox-rs", "모델을 내렸습니다 — 이제 index 를 실행할 수 있습니다");
                }
            }
            "reload" => {
                if let Some(w) = &self.worker {
                    w.start();
                    w.ensure_started();
                }
            }
            "info" => self.show_info(),
            "exit" => self.quit(),
            _ => {}
        }
    }

    // 화면 열기 — 범위 탐색기가 있으면 그 옆에, 없으면 첫 범위 폴더를 탐색기로 연다
    fn open_panel(&mut self) {
        if self.panel.visible {
            self.panel.activate();
            return;
        }
        let wins = self.monitor.in_scope_windows();
        if let Some((h, f)) = wins.first().cloned() {
            self.panel.forget_closed(h);
            self.on_folder(h, Some(f));
            self.panel.activate();
            return;
        }
        let first = self.scope.lock().unwrap().active.first().cloned();
        match first {
            None => self.tray.notify("RAGSearchBox-rs", SCOPE_WARN),
            Some(t) => {
                crate::info!("app", "화면 열기: 범위 폴더를 탐색기로 연다 — {}", t);
                if win::open_explorer(&t).is_err() {
                    self.tray.notify("RAGSearchBox-rs", &format!("탐색기를 열지 못했습니다: {}", t));
                }
            }
        }
    }

    fn start_watcher(&mut self) {
        if self.watcher.is_some() {
            return;
        }
        let roots = match &self.jobs {
            Some(j) => j.roots.clone(),
            None => return,
        };
        let p = self.proxy.clone();
        let w = FolderWatcher::new(&roots, Arc::new(move |b| post(&p, UserEvent::Watch(b))), self.s.autoindex_quiet_s, self.s.autoindex_rescan_min);
        w.start();
        self.watcher = Some(w);
    }

    fn stop_watcher(&mut self) {
        if let Some(w) = self.watcher.take() {
            w.stop();
        }
    }

    fn on_autoindex(&mut self, kind: &str, msg: &str) {
        if kind == "doc" {
            if self.panel.visible {
                self.panel.notice(msg);
                self.refresh_scope();
            }
            return;
        }
        crate::info!("app", "자동 인덱싱 알림({}): {}", kind, msg);
        self.tray.notify("RAGSearchBox-rs", msg);
        self.update_tooltip();
    }

    fn on_tray_autoindex(&mut self, name: &str) {
        if self.jobs.is_none() {
            self.tray.notify("RAGSearchBox-rs", "자동 인덱싱할 폴더가 없습니다 — [Scope] Folders 를 지정하세요");
            return;
        }
        if name == "index_now" {
            let n = self.jobs.as_mut().unwrap().run_now();
            self.tray.notify("RAGSearchBox-rs", &format!("폴더 {}개를 인덱싱합니다", n));
        } else {
            let on = !self.jobs.as_ref().unwrap().enabled;
            self.jobs.as_mut().unwrap().set_enabled(on);
            if on {
                self.start_watcher();
            } else {
                self.stop_watcher();
            }
            if let Err(e) = settings::save_ini_value(&self.s.ini_path, "AutoIndex", "Enabled", if on { "1" } else { "0" }) {
                crate::warn!("app", "자동 인덱싱 설정을 저장하지 못했다: {}", e);
            }
            self.tray.notify("RAGSearchBox-rs", &format!("자동 인덱싱을 {}", if on { "켰습니다" } else { "껐습니다" }));
        }
        self.update_tooltip();
    }

    fn make_jobs(&self) -> Option<IndexJobs> {
        let roots = self.s.scope_folders.clone();
        if roots.is_empty() || self.cmd.is_empty() {
            return None;
        }
        let p = self.proxy.clone();
        let p2 = self.proxy.clone();
        Some(IndexJobs::new(&self.s, self.worker.clone(), &roots, &self.cmd,
                            Arc::new(move |m| post(&p, UserEvent::Job(m))),
                            Box::new(move |k: &str, m: &str| post(&p2, UserEvent::AutoIndex(k.into(), m.into())))))
    }

    //------------------------------------------------------------------
    // 트레이 툴팁·메뉴 제목·체크 표시 갱신
    //------------------------------------------------------------------
    fn update_tooltip(&mut self) {
        self.t_tooltip = Instant::now();
        {
            let mut sc = self.scope.lock().unwrap();
            if sc.refresh(false) && sc.usable() {
                crate::info!("app", "범위 폴더가 연결되었다: {:?}", sc.active);
            }
        }
        let usable = self.scope.lock().unwrap().usable();
        let mut text = if !usable {
            SCOPE_WARN.to_string()
        } else {
            match &self.worker {
                None => self.worker_error.clone().unwrap_or("SimpleRAG 없음".into()),
                Some(w) => w.status_text(),
            }
        };
        if let Some(j) = &self.jobs {
            let extra = j.status_text();
            if !extra.is_empty() {
                text = format!("{} · {}", text, extra);
            }
        }
        let tip = format!("RAGSearchBox-rs — {}", text);
        if tip != self.last_tooltip {
            self.last_tooltip = tip.clone();
            self.tray.set_tooltip(&tip);
        }
        let label = if !usable {
            "범위 폴더 미설정".to_string()
        } else {
            match &self.worker {
                None => "SimpleRAG 없음".into(),
                Some(w) => {
                    let mut l = w.state_label().to_string();
                    let extra = self.jobs.as_ref().map(|j| j.short_state()).unwrap_or_default();
                    if extra.starts_with("인덱싱 중") || extra.starts_with("인덱스 확인 중") {
                        l = extra;
                    } else if !extra.is_empty() {
                        l = format!("{} · {}", l, extra);
                    }
                    l
                }
            }
        };
        self.tray.set_title(&format!("RAGSearchBox-rs — {}", label));
        let (ai, now) = match &self.jobs {
            Some(j) => (j.enabled, j.has_waiting()),
            None => (false, false),
        };
        self.tray.set_flags(ai, now);
    }

    // ── 설정 창 ─────────────────────────────────────────

    fn show_info(&mut self) {
        let was = self.info.visible;
        self.info.visible = true;
        self.refresh_info();
        if !was {
            let (cx, cy) = win::cursor_pos();
            let (_, _, r, b) = win::work_area_at(cx, cy);
            let size = self.info.window.outer_size();
            self.info.window.set_outer_position(tao::dpi::PhysicalPosition::new(r - size.width as i32 - 16, b - size.height as i32 - 16));
        }
        self.info.window.set_visible(true);
        self.info.window.set_focus();
        win::bring_to_front(self.info.window.hwnd());
    }

    //------------------------------------------------------------------
    // 설정 창 값 — 모델·백엔드·폴더·문서 수·자동 시작·로그 폴더
    //------------------------------------------------------------------
    fn info_json(&mut self) -> Value {
        let mut info = json!({"log_dir": log::log_dir()});
        if let Some(w) = &self.worker {
            let up = w.availability() != "down";
            info["model_up"] = json!(up);
            if up {
                for (k, v) in w.info().as_object().unwrap() {
                    info[k] = v.clone();
                }
            }
        }
        info["autorun"] = json!(win::autorun_value().is_some());
        let folders = match &self.jobs {
            Some(j) => j.roots.clone(),
            None => {
                let sc = self.scope.lock().unwrap();
                if sc.active.is_empty() { self.s.scope_folders.clone() } else { sc.active.clone() }
            }
        };
        if !folders.is_empty() {
            if let Some(keys) = self.state_keys() {
                let bases: Vec<String> = folders.iter().map(|f| format!("{}\\", scope::norm(f).trim_end_matches('\\'))).collect();
                info["docs"] = json!(keys.iter().filter(|k| bases.iter().any(|b| k.starts_with(b))).count());
            }
        }
        info["folders"] = json!(folders);
        info["action_msg"] = json!(self.info.action_msg);
        info["folder_msg"] = json!(self.info.folder_msg);
        // 동의어 칸 — 후보 만들기가 끝났으면 그 소식을 결과 글로 옮긴다
        if let Some(m) = self.info.syn_done.lock().unwrap().take() {
            self.info.syn_msg = m;
        }
        if let Some(home) = self.data_home() {
            let (rows, n_left) = synonyms::pending(&home, synonyms::SHOW_ROWS);
            let (n_miss, n_ok) = synonyms::counts(&home);
            info["syn"] = json!({"rows": rows, "n_left": n_left, "approved": synonyms::approved_pairs(&home),
                                 "note": synonyms::note(n_miss, n_left, n_ok, &self.info.syn_msg)});
        }
        info
    }

    //------------------------------------------------------------------
    // 설정 창 [후보 만들기] — 동의어 후보(synonym_candidates.json)를 뒤에서 만든다 (Python _suggest_synonyms 와 같다)
    //=> 인덱스는 한 프로세스만 열 수 있어서, 워커 상태에 따라 길을 고른다(자동 인덱싱과 같은 방식).
    //    1) 워커가 떠 있으면 워커에게 "/suggest-synonyms" — 올려 둔 1.7B 로 고르고, 최근 놓친 질문 60개까지만
    //    2) 내려가 있으면 "suggest-synonyms" 명령을 따로 돌리고, 그동안 워커가 뜨지 않게 막는다(hold)
    //    3) 끝나면 결과 줄("@index {json}")로 소식을 syn_done 에 남긴다 — 후보 파일이 바뀌어 줄이 새로 그려진다
    //   돌려줌: 안내 글
    //------------------------------------------------------------------
    fn suggest_synonyms(&mut self) -> String {
        use std::sync::atomic::Ordering;
        if self.cmd.is_empty() {
            return "SimpleRAG 를 찾지 못해 후보를 만들 수 없습니다".into();
        }
        if self.info.syn_running.load(Ordering::SeqCst) {
            return "후보를 만드는 중입니다 — 끝나면 여기에 보입니다".into();
        }
        self.info.syn_running.store(true, Ordering::SeqCst);
        let (done, running) = (self.info.syn_done.clone(), self.info.syn_running.clone());
        // 결과를 소식으로 남긴다(워커 스레드·뒤 스레드에서 불린다)
        let finish = move |res: Value| {
            let msg = synonym_result_msg(&res);
            crate::info!("app", "동의어 후보 만들기 끝: {}", msg);
            *done.lock().unwrap() = Some(msg);
            running.store(false, Ordering::SeqCst);
        };
        let how = match &self.worker {
            Some(w) if w.availability() != "down" => {
                crate::info!("app", "동의어 후보 만들기: 워커에게 /suggest-synonyms");
                w.command("/suggest-synonyms".into(), Box::new(finish));
                "워커가 1.7B 로 — 최근 질문 60개까지, 1~3분. 그동안 온 질문은 끝난 뒤에 답합니다"
            }
            _ => {
                let mut cmd = self.cmd.clone();
                cmd.push("suggest-synonyms".into());
                crate::info!("app", "동의어 후보 만들기: {}", cmd.join(" "));
                let worker = self.worker.clone();
                std::thread::spawn(move || {
                    // 명령이 인덱스를 쥔 동안 워커가 뜨면 잠금 충돌이므로 막았다가 푼다
                    if let Some(w) = &worker {
                        w.hold(Some("동의어 후보 만드는 중".into()));
                    }
                    let mut c = std::process::Command::new(&cmd[0]);
                    c.args(&cmd[1..]).stdin(std::process::Stdio::null());
                    if let Some(d) = std::path::Path::new(&cmd[0]).parent() {
                        c.current_dir(d);
                    }
                    {
                        use std::os::windows::process::CommandExt;
                        c.creation_flags(0x0800_0000);
                    }
                    let res = match c.output() {
                        Ok(o) => parse_result_line(&(String::from_utf8_lossy(&o.stdout).into_owned() + &String::from_utf8_lossy(&o.stderr))),
                        Err(e) => json!({"ok": false, "why": e.to_string()}),
                    };
                    if let Some(w) = &worker {
                        w.hold(None);
                    }
                    finish(res);
                });
                "모델이 내려가 있어 따로 실행 — 1~3분"
            }
        };
        format!("후보를 만드는 중입니다 ({})", how)
    }

    fn refresh_info(&mut self) {
        self.t_info = Instant::now();
        if !self.info.ready {
            return;
        }
        let j = self.info_json();
        let _ = self.info.webview.evaluate_script(&format!("rsb.update({})", j));
    }

    //------------------------------------------------------------------
    // 설정 창 화면이 보낸 메시지
    //------------------------------------------------------------------
    fn on_info_ipc(&mut self, raw: &str) {
        let m: Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(_) => return,
        };
        match m["type"].as_str().unwrap_or("") {
            "ready" => {
                self.info.ready = true;
                self.refresh_info();
            }
            "unload" => {
                self.info.action_msg = match &self.worker {
                    None => "SimpleRAG 를 찾지 못해 내릴 모델이 없습니다".into(),
                    Some(_) => {
                        self.on_tray("unload");
                        "모델을 내렸습니다 — 이제 index 명령을 실행할 수 있습니다".into()
                    }
                };
            }
            "reload" => {
                self.info.action_msg = match &self.worker {
                    None => "SimpleRAG 를 찾지 못해 올릴 모델이 없습니다".into(),
                    Some(_) => {
                        self.on_tray("reload");
                        "모델을 올리는 중입니다 (10초 안팎)".into()
                    }
                };
            }
            "autorun" => {
                self.info.action_msg = if win::autorun_value().is_some() {
                    match win::autorun_disable() { Ok(_) => "자동 시작을 껐습니다".into(), Err(e) => e }
                } else {
                    match win::autorun_enable() { Ok(_) => "로그인할 때 자동으로 시작합니다".into(), Err(e) => e }
                };
            }
            "add_folder" => {
                if let Some(p) = rfd::FileDialog::new().set_title("인덱싱할 폴더 고르기").pick_folder() {
                    let p = p.to_string_lossy().into_owned();
                    let cur = self.current_folders();
                    if cur.iter().any(|f| f.to_lowercase() == p.to_lowercase()) {
                        self.info.folder_msg = "이미 들어 있는 폴더입니다".into();
                    } else {
                        let mut v = cur;
                        v.push(p);
                        self.info.folder_msg = self.set_folders(v).unwrap_or_else(|e| e);
                    }
                }
            }
            "remove_folder" => {
                let target = m["folder"].as_str().unwrap_or("").to_string();
                let v: Vec<String> = self.current_folders().into_iter().filter(|f| *f != target).collect();
                self.info.folder_msg = self.set_folders(v).unwrap_or_else(|e| e);
            }
            "folder_msg" => self.info.folder_msg = m["msg"].as_str().unwrap_or("").to_string(),
            "rescan" => {
                self.info.folder_msg = match self.jobs.as_mut() {
                    None => "자동 인덱싱할 폴더가 없습니다".into(),
                    Some(j) if !j.enabled => "자동 인덱싱이 꺼져 있습니다 — 트레이에서 켜면 확인합니다".into(),
                    Some(j) => {
                        j.rescan_all();
                        "폴더를 다시 확인합니다".into()
                    }
                };
                self.update_tooltip();
            }
            "open_logs" => {
                let _ = win::open_explorer(&log::log_dir());
            }
            // 동의어 탭 — [맞음] · [아님] · [직접 추가] · [빼기] · [후보 만들기]
            "syn_approve" | "syn_reject" | "syn_remove" => {
                let (w, t) = (m["word"].as_str().unwrap_or(""), m["term"].as_str().unwrap_or(""));
                self.info.syn_msg = match self.data_home() {
                    None => "SimpleRAG 데이터 폴더를 찾지 못했습니다".into(),
                    Some(home) if m["type"] == "syn_approve" => synonyms::approve(&home, w, t).1,
                    Some(home) if m["type"] == "syn_reject" => synonyms::reject(&home, w, t).1,
                    Some(home) => synonyms::remove(&home, w, t).1,
                };
            }
            "syn_msg" => self.info.syn_msg = m["msg"].as_str().unwrap_or("").to_string(),
            "syn_suggest" => self.info.syn_msg = self.suggest_synonyms(),
            "open" => self.on_tray("open"),
            "hide" => {
                self.info.visible = false;
                self.info.window.set_visible(false);
                return;
            }
            _ => {}
        }
        self.refresh_info();
    }

    fn current_folders(&self) -> Vec<String> {
        match &self.jobs {
            Some(j) => j.roots.clone(),
            None => self.s.scope_folders.clone(),
        }
    }

    //------------------------------------------------------------------
    // 인덱싱 폴더 바꾸기 — INI 저장, 범위·자동 인덱싱 다시 만들기
    //------------------------------------------------------------------
    fn set_folders(&mut self, folders: Vec<String>) -> Result<String, String> {
        let mut clean: Vec<String> = vec![];
        for f in folders {
            let p = std::path::absolute(&f).map(|x| x.to_string_lossy().into_owned()).unwrap_or(f);
            if !clean.iter().any(|c| c.to_lowercase() == p.to_lowercase()) {
                clean.push(p);
            }
        }
        if clean.is_empty() {
            return Err("폴더가 하나는 있어야 합니다".into());
        }
        if let Some(m) = clean.iter().find(|p| !std::path::Path::new(p).is_dir()) {
            return Err(format!("없는 폴더입니다: {}", m));
        }
        if let Some(j) = &self.jobs {
            let st = j.short_state();
            if st.starts_with("인덱싱 중") || st.starts_with("인덱스 확인 중") {
                return Err("인덱싱이 돌고 있습니다 — 끝난 뒤 다시 바꾸세요".into());
            }
        }
        settings::save_ini_value(&self.s.ini_path, "Scope", "Folders", &clean.join(";")).map_err(|e| {
            crate::warn!("app", "범위 폴더를 저장하지 못했다: {}", e);
            e
        })?;
        crate::info!("app", "인덱싱 폴더를 바꿨다: {}", clean.join(";"));
        self.s.scope_folders = clean.clone();
        {
            let mut sc = self.scope.lock().unwrap();
            sc.configured = clean;
            sc.refresh(true);
        }
        let was_enabled = self.jobs.as_ref().map(|j| j.enabled).unwrap_or(self.s.autoindex_enabled);
        self.stop_watcher();
        self.jobs = self.make_jobs();
        if let Some(j) = self.jobs.as_mut() {
            j.enabled = was_enabled;
        }
        if was_enabled {
            self.start_watcher();
        }
        if let Some(w) = &self.worker {
            if self.scope.lock().unwrap().usable() {
                w.start();
            }
        }
        self.update_tooltip();
        if self.jobs.is_some() && !was_enabled {
            return Ok("저장했습니다. 자동 인덱싱이 꺼져 있어 인덱스는 그대로입니다".into());
        }
        Ok("저장했습니다. 새 폴더는 인덱싱하고 뺀 폴더 문서는 인덱스에서 뺍니다".into())
    }

    //------------------------------------------------------------------
    // 끝내기 — 폴더 감시 · 감시 · 워커 · 트레이 · 창 순서로 정리
    //------------------------------------------------------------------
    fn quit(&mut self) {
        if self.quitting {
            return;
        }
        self.quitting = true;
        crate::info!("app", "종료를 시작한다");
        self.stop_watcher();
        self.monitor.stop();
        if let Some(w) = &self.worker {
            w.shutdown();
        }
        self.tray.stop();
        self.panel.close();
        self.info.window.set_visible(false);
        crate::info!("app", "종료 완료");
    }
}

//------------------------------------------------------------------
// --autorun on|off|status — 부모 콘솔에 결과를 쓰고(없으면 알림 창) 끝낸다
//------------------------------------------------------------------
fn autorun_cli(action: &str) -> i32 {
    use windows::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
    let attached = unsafe { AttachConsole(ATTACH_PARENT_PROCESS) }.is_ok();
    let out = |text: &str, modal: bool| {
        crate::info!("autorun", "{}", text);
        if attached {
            if let Ok(mut f) = std::fs::OpenOptions::new().write(true).open("CONOUT$") {
                use std::io::Write;
                let _ = writeln!(f, "{}", text);
            }
        } else if modal {
            rfd::MessageDialog::new().set_title("RAGSearchBox-rs").set_description(text).show();
        }
    };
    match action.to_lowercase().as_str() {
        "on" => match win::autorun_enable() {
            Ok(v) => {
                out(&format!("자동 시작 등록: {}", v), true);
                0
            }
            Err(e) => {
                out(&e, true);
                2
            }
        },
        "off" => match win::autorun_disable() {
            Ok(m) => {
                out(&m, true);
                0
            }
            Err(e) => {
                out(&e, true);
                2
            }
        },
        "status" => match win::autorun_value() {
            Some(v) => {
                out(&format!("등록됨: {}", v), false);
                0
            }
            None => {
                out("등록되어 있지 않습니다", false);
                1
            }
        },
        _ => {
            out("사용법: RAGSearchBox-rs.exe --autorun on|off|status", false);
            2
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(|a| a == "--autorun").unwrap_or(false) {
        std::process::exit(autorun_cli(args.get(1).map(|s| s.as_str()).unwrap_or("")));
    }
    let s = settings::load(None);
    log::setup(&s.log_level);
    let _mutex = match win::single_instance(MUTEX_NAME) {
        Some(h) => h,
        None => {
            crate::info!("main", "이미 실행 중이라 종료한다 (뮤텍스 {})", MUTEX_NAME);
            std::process::exit(1);
        }
    };
    crate::info!("main", "RAGSearchBox-rs 시작 — 설정 {}", s.ini_path);

    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy: Proxy = Arc::new(Mutex::new(event_loop.create_proxy()));
    let icon = Icon::from_resource(1, None).ok();

    // WebView2 사용자 데이터는 %LOCALAPPDATA%\RAGSearchBox-rs\webview (exe 옆이 쓰기 금지여도 되게)
    let data = std::path::Path::new(&log::log_dir()).parent().map(|p| p.join("webview")).unwrap_or_default();
    let ctx: &'static mut WebContext = Box::leak(Box::new(WebContext::new(Some(data))));

    // 패널 창
    let pwin = WindowBuilder::new().with_title("RAGSearchBox-rs").with_visible(false).with_window_icon(icon.clone())
        .with_inner_size(tao::dpi::PhysicalSize::new(s.win_width as u32, 700)).build(&event_loop).expect("패널 창");
    let font = font_px(s.font_size);
    let moai = {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD.encode(MOAI_PNG)
    };
    let html = PANEL_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", &moai).replace("__SCOPE__", &s.panel_search_scope);
    let p1 = proxy.clone();
    let pview = WebViewBuilder::with_web_context(ctx).with_html(html)
        .with_ipc_handler(move |req| post(&p1, UserEvent::PanelIpc(req.into_body())))
        .build(&pwin).expect("패널 화면");

    // 설정 창
    let iwin = WindowBuilder::new().with_title("RAGSearchBox-rs 설정").with_visible(false).with_window_icon(icon)
        .with_resizable(false).with_inner_size(tao::dpi::LogicalSize::new(600.0, 640.0)).build(&event_loop).expect("설정 창");
    let p2 = proxy.clone();
    let iview = WebViewBuilder::with_web_context(unsafe { &mut *(ctx as *mut WebContext) })
        .with_html(INFO_HTML.replace("__FONT_PX__", &font))
        .with_ipc_handler(move |req| post(&p2, UserEvent::InfoIpc(req.into_body())))
        .build(&iwin).expect("설정 화면");

    // 워커
    let (worker, worker_error, cmd) = match settings::find_worker_cmd(&s) {
        Ok((cmd, why)) => {
            crate::info!("app", "워커 명령: {} ({})", cmd.join(" "), why);
            let p = proxy.clone();
            let w = Worker::new(cmd.clone(), vec![("SIMPLERAG_RELEVANCE_MIN".into(), s.relevance_min.clone()), ("SIMPLERAG_EVIDENCE_FULL".into(), "1".into())],
                                Box::new(move |e| post(&p, UserEvent::Worker(e))), s.no_stream, s.start_timeout_s, s.answer_timeout_s, s.restart_max, s.idle_unload_min);
            (Some(w), None, cmd)
        }
        Err(e) => {
            crate::error!("app", "{}", e);
            (None, Some(e), vec![])
        }
    };

    let scope = Arc::new(Mutex::new(Scope::new(&s.scope_folders, s.scope_recheck_min)));
    let pt = proxy.clone();
    let tray = Tray::start(Box::new(move |name: &str| post(&pt, UserEvent::Tray(name.to_string()))));
    let pm = proxy.clone();
    let monitor = Monitor::start(scope.clone(), s.folder_poll_ms, s.panel_enabled, Arc::new(move |e| post(&pm, UserEvent::Monitor(e))));
    let history_path = history::default_path();
    let mut app = App {
        panel: Panel::new(pwin, pview, &s),
        info: InfoWin { window: iwin, webview: iview, visible: false, ready: false, action_msg: String::new(), folder_msg: String::new(),
                       syn_msg: String::new(), syn_done: Arc::new(Mutex::new(None)), syn_running: Arc::new(std::sync::atomic::AtomicBool::new(false)) },
        history: history::load(&history_path),
        history_path,
        s,
        proxy,
        scope,
        worker,
        worker_error,
        cmd,
        jobs: None,
        watcher: None,
        tray,
        monitor,
        answer_hwnd: 0,
        state_cache: (None, vec![]),
        last_tooltip: String::new(),
        t_tooltip: Instant::now(),
        t_info: Instant::now(),
        quitting: false,
        selftest_done: false,
    };
    app.jobs = app.make_jobs();
    app.start();

    let panel_id = app.panel.window.id();
    let info_id = app.info.window.id();
    event_loop.run(move |event, _, flow| {
        *flow = ControlFlow::WaitUntil(Instant::now() + PUMP);
        match event {
            Event::UserEvent(e) => app.handle(e),
            Event::NewEvents(tao::event::StartCause::ResumeTimeReached { .. }) => app.pump(),
            Event::WindowEvent { window_id, event: WindowEvent::ScaleFactorChanged { .. }, .. } if window_id == panel_id => {
                app.panel.dpi_changed_at = Some(Instant::now());
            }
            Event::WindowEvent { window_id, event: WindowEvent::CloseRequested, .. } => {
                if window_id == panel_id {
                    app.panel.close_by_user();
                } else if window_id == info_id {
                    app.info.visible = false;
                    app.info.window.set_visible(false);
                }
            }
            _ => {}
        }
        if app.quitting {
            *flow = ControlFlow::Exit;
        }
    });
}
