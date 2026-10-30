//------------------------------------------------------------------
// RAGSearchBox-rs — 트레이 상주 앱 진입점 (Python main.py · app.py)
//=> 탐색기에서 지정 폴더를 열면 오른쪽에 대화 패널이 붙고, 질문은 상주 워커(simplerag-rs chat)가 답한다.
//   구성: 트레이(자기 스레드) · 탐색기 감시(자기 스레드) · 워커 관리(자기 스레드) · 폴더 감시(폴더마다 스레드)
//         · 패널/설정 창(메인 스레드, WebView2). 다른 스레드의 일은 모두 사용자 이벤트로 메인에 넘어온다.
//------------------------------------------------------------------
#![windows_subsystem = "windows"]

mod browse_win;
mod explorer;
mod folder_icon;
mod history;
mod jobs;
mod log;
mod monitor;
mod panel;
mod people;
mod precision;
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
use browse_win::Browse;
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
const BROWSE_HTML: &str = include_str!("../ui/browse.html");
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
    BrowseIpc(String),
    // 폴더 아이콘 맞추기가 끝났다 — 실패 안내 목록(성공이면 비어 있음)
    IconNotes(Vec<String>),
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
    // 사람 탭 마지막 결과 글 · meta people 명령 뒤 스레드가 남긴 소식 · 도는 중인가 (동의어 칸과 같은 방식)
    ppl_msg: String,
    ppl_done: Arc<Mutex<Option<String>>>,
    ppl_running: Arc<std::sync::atomic::AtomicBool>,
    // 마지막 정밀 모드 바꾸기 결과(체크 아래 안내 줄)
    prec_msg: String,
    // 마지막 이어서 묻기(멀티턴) 바꾸기 결과
    mt_msg: String,
}

//------------------------------------------------------------------
// 앱 — 모든 구성요소
//------------------------------------------------------------------
// 문서 업로드 한도(문서 업로드 설계서 §10) · 올릴 수 있는 확장자(SimpleRAG indexer TEXT_EXTS 와 같다)
const UPLOAD_MAX_FILES: usize = 5;
const UPLOAD_MAX_MB: u64 = 30;
const UPLOAD_EXTS: &[&str] = &["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "hwp", "hwpx", "txt", "md", "html", "htm", "py", "ipynb"];

//------------------------------------------------------------------
// 대화 id → 업로드 폴더 이름 (SimpleRAG uploads::safe_id 와 같은 규칙)
//------------------------------------------------------------------
fn upload_safe_id(conv: &str) -> Option<String> {
    let s: String = conv.chars().map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-' { c } else { '_' }).collect();
    let s = s.trim_matches(|c| c == '.' || c == '_').to_string();
    if s.is_empty() { None } else { Some(s) }
}

//------------------------------------------------------------------
// 업로드 진행 글 → (진행률 %, 글) — "[upload] 임베딩 64/128" · "[upload] 글자 뽑는 중 — …"
//------------------------------------------------------------------
fn upload_progress(line: &str) -> Option<(u32, String)> {
    let rest = line.strip_prefix("[upload] ")?;
    if let Some(n) = rest.strip_prefix("임베딩 ") {
        let (a, b) = n.split_once('/')?;
        let (a, b): (f64, f64) = (a.trim().parse().ok()?, b.trim().parse().ok()?);
        // 글자 뽑기를 10% 로 치고 임베딩이 나머지 90%
        return Some(((10.0 + 89.0 * a / b.max(1.0)) as u32, format!("임베딩 {}/{}", a, b)));
    }
    Some((5, rest.split(" — ").next().unwrap_or(rest).to_string()))
}

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
    // 업로드 명령 결과(워커 스레드가 넣고 pump 가 패널로 옮긴다) · 지금 올리는 중인 파일 수
    up_done: Arc<Mutex<Vec<Value>>>,
    up_running: usize,
    answer_hwnd: isize,
    state_cache: (Option<std::time::SystemTime>, Vec<String>),
    last_tooltip: String,
    t_tooltip: Instant,
    t_info: Instant,
    quitting: bool,
    selftest_done: bool,
    // 탐색기 모드(탐색기 모드 설계서) — MoAI 탐색기 창 · 패널에 지금 올린 화면(explorer | rag)
    browse: Browse,
    panel_mode: String,
    // AI 답(RAG)을 어느 화면이 물었나 — true 면 탐색기 창, false 면 패널
    ask_to_browse: bool,
    // /find 결과(워커 스레드가 넣고 pump 가 화면으로 옮긴다) — (탐색기 창으로?, 결과)
    find_done: Arc<Mutex<Vec<(bool, Value)>>>,
    // 폴더 아이콘 맞추기 결과 안내(설정 창 폴더 메모에)
    icon_msg: String,
    // 화면 모드를 바꾼 결과 안내(설정 창 화면 모드 칸에)
    mode_msg: String,
    moai_b64: String,
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

//------------------------------------------------------------------
// 패널 화면 고르기 — 탐색기 모드면 browse.html(좁은 배치), RAG 모드면 대화 화면 panel.html
//------------------------------------------------------------------
fn panel_html(mode: &str, s: &Settings, moai: &str) -> String {
    let font = font_px(s.font_size);
    if mode == "explorer" {
        BROWSE_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", moai).replace("__LAYOUT__", "panel")
    } else {
        PANEL_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", moai).replace("__SCOPE__", &s.panel_search_scope)
    }
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
            self.tray.notify("MoAI-S 설정 확인", &self.s.warnings.iter().take(3).cloned().collect::<Vec<_>>().join("\n"));
        }
        if let Some(e) = &self.worker_error {
            self.tray.notify("MoAI-S", e);
        }
        if self.s.searchbox_trigger {
            crate::warn!("app", "[Trigger] SearchBox=1(검색창 ? 질문)은 Rust 판에서 지원하지 않는다 — 폴더 패널만 동작한다");
        }
        let usable = self.scope.lock().unwrap().usable();
        if !usable {
            crate::warn!("app", "{}", SCOPE_WARN);
            self.tray.notify("MoAI-S", SCOPE_WARN);
        } else if let Some(w) = &self.worker {
            w.start();
            if self.s.start_mode == "boot" {
                crate::info!("app", "StartMode=boot — 모델을 미리 올린다");
                w.ensure_started();
            }
        }
        // 기록에 없는 대화의 업로드 폴더 정리(지우다 실패했거나 기록 파일을 지운 경우)
        self.gc_uploads();
        if let Some(fixed) = win::autorun_fix_path() {
            crate::info!("app", "자동 시작 경로를 고쳤다: {}", fixed);
        }
        if self.jobs.as_ref().map(|j| j.enabled).unwrap_or(false) {
            self.start_watcher();
        }
        // 인덱싱 폴더 아이콘 — ini 를 손으로 고친 경우까지 시작할 때 맞춰 본다
        self.sync_icons();
        // 자체 시험(RSB_SELFTEST_BROWSE) — 시작하자마자 MoAI 탐색기 창을 연다(값이 1 이 아니면 그 검색어로 찾는다)
        let selftest = std::env::var("RSB_SELFTEST_BROWSE").unwrap_or_default();
        if !selftest.is_empty() {
            crate::info!("app", "자체 시험: 탐색기 창 열기 ({})", selftest);
            let q = if selftest == "1" { None } else { Some(selftest) };
            self.open_browse(q.as_deref(), None, Value::Null);
        } else if !std::env::args().any(|a| a == "--tray") {
            // 직접 실행하면 화면을 바로 띄운다(트레이 "열기" 와 같다) — 로그인 자동 시작(--tray)은 트레이에만
            crate::info!("app", "시작 화면 띄우기 ({} 모드)", self.s.ui_mode);
            if self.s.ui_mode == "explorer" {
                self.open_browse(None, None, Value::Null);
            } else {
                self.open_panel();
            }
        }
        self.update_tooltip();
        crate::info!("app", "시작 완료 (범위 폴더 {}개, 화면 {} 모드)", self.scope.lock().unwrap().active.len(), self.s.ui_mode);
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
        self.pump_uploads();
        self.pump_finds();
    }

    // /find 결과를 물은 화면으로
    fn pump_finds(&mut self) {
        let done: Vec<(bool, Value)> = std::mem::take(&mut *self.find_done.lock().unwrap());
        for (to_browse, r) in done {
            if to_browse {
                self.browse.call("results", r);
            } else {
                self.panel.call("results", r);
            }
        }
    }

    //------------------------------------------------------------------
    // 폴더 아이콘 맞추기 (폴더 아이콘 설계서 §4) — 셸 알림·네트워크 폴더가 느릴 수 있어 자기 스레드에서
    //------------------------------------------------------------------
    fn sync_icons(&self) {
        let folders = self.current_folders();
        let enabled = self.s.folder_icon;
        let p = self.proxy.clone();
        std::thread::spawn(move || {
            let notes = folder_icon::sync(&folders, enabled);
            post(&p, UserEvent::IconNotes(notes));
        });
    }

    // 탐색기 창 · 패널(탐색기 모드)에 인덱싱 폴더 · 쪽 열기 설정을 알린다
    fn push_roots(&mut self) {
        let mut roots = browse_win::roots_json(&self.current_folders());
        // 인덱싱 폴더마다 문서 수 — 상태 파일의 문서 키를 경로 앞부분으로 센다(파일이 그대로면 다시 읽지 않는다)
        if let Some(keys) = self.state_keys() {
            if let Some(arr) = roots.as_array_mut() {
                for r in arr.iter_mut() {
                    let p = r["path"].as_str().unwrap_or("").trim_end_matches(['\\', '/']).to_lowercase() + "\\";
                    let n = keys.iter().filter(|k| k.starts_with(&p)).count();
                    r["count"] = json!(n);
                }
            }
        }
        self.browse.call("setRoots", roots.clone());
        self.browse.call("setPageClick", json!(self.s.page_click));
        if self.panel_mode == "explorer" {
            self.panel.call("setRoots", roots);
            self.panel.call("setPageClick", json!(self.s.page_click));
        }
    }

    //------------------------------------------------------------------
    // MoAI 탐색기 창 열기 — q 가 있으면 그 검색을 바로 돌리고(크게 보기), select 쪽을 고른다
    //------------------------------------------------------------------
    fn open_browse(&mut self, q: Option<&str>, folder: Option<String>, select: Value) {
        self.push_roots();
        self.browse.show();
        if let Some(q) = q.filter(|q| !q.trim().is_empty()) {
            self.browse.js(format!("rsb.runQuery({}, {}, {})", json!(q), json!(folder), select));
        }
        if let Some(w) = &self.worker {
            if self.s.start_mode != "lazy" {
                w.ensure_started();
            }
        }
    }

    //------------------------------------------------------------------
    // 패널 화면 바꾸기(탐색기 ↔ RAG) — 이번 실행 동안만. 설정 창에서 바꾸면 ini 에도 남는다
    //------------------------------------------------------------------
    //------------------------------------------------------------------
    // 화면 모드 저장 — ini [UI] Mode 에 남기고 설정 창 안내 글을 고친다(값 rag = 채팅 모드)
    //------------------------------------------------------------------
    fn save_ui_mode(&mut self, mode: &str) {
        self.s.ui_mode = mode.to_string();
        self.mode_msg = match settings::save_ini_value(&self.s.ini_path, "UI", "Mode", mode) {
            Ok(_) => if mode == "rag" { "채팅 모드 — 질문하면 AI 가 문서를 근거로 답합니다".into() }
                     else { "탐색기 모드 — 문서 · 쪽 · 내용을 찾아 목록으로 보여 줍니다".into() },
            Err(e) => e,
        };
    }

    //------------------------------------------------------------------
    // 화면 모드 바꾸기 ([채팅 모드] · [탐색기 모드] 버튼)
    //=> 모드를 저장하고 지금 화면을 닫은 뒤 그 모드의 화면을 연다.
    //    · 채팅 모드: 탐색기 창을 숨기고 탐색기 옆 대화 패널(트레이 "열기" 와 같다)
    //    · 탐색기 모드: 대화 패널을 숨기고 MoAI 탐색기 창
    //------------------------------------------------------------------
    fn switch_ui_mode(&mut self, mode: &str) {
        crate::info!("app", "화면 모드 바꾸기: {}", mode);
        self.save_ui_mode(mode);
        self.set_panel_mode(mode);
        if mode == "rag" {
            self.browse.hide();
            self.open_panel();
        } else {
            self.panel.hide(true);
            self.open_browse(None, None, Value::Null);
        }
    }

    //------------------------------------------------------------------
    // 인덱싱 폴더 추가 — 폴더 고르기 창을 띄워 넣는다. 돌려줌: 안내 글(취소하면 빈 글)
    //------------------------------------------------------------------
    fn add_folder_dialog(&mut self) -> String {
        let p = match rfd::FileDialog::new().set_title("인덱싱할 폴더 고르기").pick_folder() {
            Some(p) => p.to_string_lossy().into_owned(),
            None => return String::new(),
        };
        let cur = self.current_folders();
        if cur.iter().any(|f| f.to_lowercase() == p.to_lowercase()) {
            return "이미 들어 있는 폴더입니다".into();
        }
        let mut v = cur;
        v.push(p);
        let msg = self.set_folders(v).unwrap_or_else(|e| e);
        self.push_roots();
        msg
    }

    fn set_panel_mode(&mut self, mode: &str) {
        if self.panel_mode == mode {
            return;
        }
        self.panel_mode = mode.to_string();
        let html = panel_html(mode, &self.s, &self.moai_b64);
        self.panel.reset_ready();
        if let Err(e) = self.panel.webview.load_html(&html) {
            crate::warn!("panel", "패널 화면을 바꾸지 못했다: {}", e);
        }
        crate::info!("panel", "패널 화면: {} 모드", mode);
    }

    //------------------------------------------------------------------
    // 탐색기 모드 화면 요청 (탐색기 창 · 패널 공통) — 처리했으면 true
    //   find · preview · preview_hide · pv_rect · open_page · reveal · list_dirs · ask_ai · big · mode
    //------------------------------------------------------------------
    fn on_find_ipc(&mut self, to_browse: bool, m: &Value) -> bool {
        match m["type"].as_str().unwrap_or("") {
            "find" => {
                // 폴더 옆 문서 수를 새로 — 그사이 자동 인덱싱으로 바뀌었을 수 있다(상태 파일이 그대로면 읽지 않는다)
                if to_browse {
                    self.push_roots();
                }
                let q = m["q"].as_str().unwrap_or("").trim().to_string();
                let folder = m["folder"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
                if q.is_empty() {
                    return true;
                }
                crate::info!("browse", "찾기: {:?} (범위 {:?})", q.chars().take(40).collect::<String>(), folder);
                let done = self.find_done.clone();
                match &self.worker {
                    None => {
                        let e = self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into());
                        done.lock().unwrap().push((to_browse, json!({"ok": false, "q": q, "why": e})));
                    }
                    Some(w) => {
                        // 이미 떠 있는 옛 워커는 /find 를 모른다 — 새로 빌드하라고 알린다
                        if w.availability() == "up" && !w.knows("find") {
                            done.lock().unwrap().push((to_browse, json!({"ok": false, "q": q,
                                "why": "워커가 탐색기 모드 찾기(/find)를 모릅니다 — simplerag-rs.exe 를 새로 빌드하세요"})));
                            return true;
                        }
                        w.ensure_started();
                        // 조건 칩에서 끈 조건 · 넓힌 기간(메타데이터 검색 설계서 §14)을 함께 넘긴다
                        let req = json!({"q": q, "folder": folder, "off": m["off"].as_array().cloned().unwrap_or_default(),
                                         "widen": m["widen"].as_i64().unwrap_or(0)});
                        let qq = q.clone();
                        w.command(format!("/find {}", worker::ascii_json(&req)), Box::new(move |mut r: Value| {
                            r["q"] = json!(qq);
                            done.lock().unwrap().push((to_browse, r));
                        }));
                    }
                }
            }
            "preview" if to_browse => {
                let path = m["path"].as_str().unwrap_or("");
                if let Some(pg) = m["page"].as_i64() {
                    self.browse.preview_pdf(path, pg);
                }
            }
            "preview_hide" if to_browse => self.browse.preview_hide(),
            "pv_rect" if to_browse => {
                let f = |k: &str| m[k].as_f64().unwrap_or(0.0);
                self.browse.set_preview_rect(f("x"), f("y"), f("w"), f("h"), m["show"].as_bool().unwrap_or(true));
            }
            "open_page" => {
                let path = m["path"].as_str().unwrap_or("").to_string();
                let r = win::open_page(&path, m["page"].as_i64());
                let msg = match r {
                    Ok(t) => t,
                    Err(e) => {
                        crate::warn!("browse", "문서를 열지 못했다: {} ({})", path, e);
                        format!("문서를 열지 못했습니다: {}", e)
                    }
                };
                if to_browse { self.browse.call("notice", json!(msg)) } else { self.panel.notice(&msg) }
            }
            "reveal" => {
                let path = m["path"].as_str().unwrap_or("");
                if let Err(e) = win::reveal(path) {
                    crate::warn!("browse", "폴더에서 보기 실패: {} ({})", path, e);
                }
            }
            "list_dirs" => {
                let path = m["path"].as_str().unwrap_or("").to_string();
                let names = browse_win::list_dirs(&path);
                let code = format!("rsb.setDirs({}, {})", json!(path), json!(names));
                if to_browse { self.browse.js(code) } else { self.panel.js(code) }
            }
            "ask_ai" => {
                let q = match one_line(m["q"].as_str().unwrap_or("")) {
                    Some(q) => q,
                    None => return true,
                };
                let folder = m["folder"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
                crate::info!("browse", "AI 답 요청: {:?} (범위 {:?})", q.chars().take(40).collect::<String>(), folder);
                self.ask_to_browse = to_browse;
                match &self.worker {
                    Some(w) => w.ask(&q, folder, None, None, false),
                    None => {
                        let e = self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into());
                        if to_browse { self.browse.call("error", json!(e)) } else { self.panel.show_error(&e) }
                    }
                }
            }
            "big" => {
                let q = m["q"].as_str().unwrap_or("").to_string();
                let folder = m["folder"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
                let select = m.get("select").cloned().unwrap_or(Value::Null);
                self.open_browse(Some(&q), folder, select);
            }
            "mode" => {
                let mode = if m["mode"] == "rag" { "rag" } else { "explorer" };
                self.set_panel_mode(mode);
            }
            // [채팅 모드] · [탐색기 모드] 버튼 — 화면 모드를 바꿔 저장하고 그 모드의 화면으로 옮겨 간다
            "switch_mode" => {
                let mode = if m["mode"] == "rag" { "rag" } else { "explorer" };
                self.switch_ui_mode(mode);
            }
            // 탐색기 창 왼쪽 아래 [폴더 추가] · [폴더 빼기] — 설정 창과 같은 일
            "add_folder" => {
                let msg = self.add_folder_dialog();
                if !msg.is_empty() {
                    self.browse.call("notice", json!(msg));
                }
            }
            "remove_folder" => {
                let target = m["folder"].as_str().unwrap_or("").to_string();
                let v: Vec<String> = self.current_folders().into_iter().filter(|f| f.to_lowercase() != target.to_lowercase()).collect();
                let msg = self.set_folders(v).unwrap_or_else(|e| e);
                self.browse.call("notice", json!(msg));
                self.push_roots();
            }
            _ => return false,
        }
        true
    }

    //------------------------------------------------------------------
    // MoAI 탐색기 창 요청
    //------------------------------------------------------------------
    fn on_browse_ipc(&mut self, raw: &str) {
        let m: Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(_) => return,
        };
        if m["type"] == "ready" {
            self.browse.on_ready();
            self.push_roots();
            return;
        }
        self.on_find_ipc(true, &m);
    }

    //------------------------------------------------------------------
    // 업로드 결과·진행을 패널로 (문서 업로드 설계서 §8)
    //=> 워커 스레드가 남긴 결과를 uploadDone 으로 넘기고, 올리는 중이면 stderr 마지막 진행 글을 % 로 바꿔 보인다
    //------------------------------------------------------------------
    fn pump_uploads(&mut self) {
        let done: Vec<Value> = std::mem::take(&mut *self.up_done.lock().unwrap());
        for r in done {
            self.up_running = self.up_running.saturating_sub(1);
            self.panel.call("uploadDone", r);
        }
        if self.up_running > 0 {
            if let Some((pct, text)) = self.worker.as_ref().and_then(|w| w.last_stderr()).and_then(|l| upload_progress(&l)) {
                self.panel.call("uploadProgress", json!({"pct": pct, "text": text}));
            }
        }
    }

    //------------------------------------------------------------------
    // 워커 데이터 폴더의 uploads 폴더
    //------------------------------------------------------------------
    fn upload_root(&self) -> Option<std::path::PathBuf> {
        Some(std::path::Path::new(&self.data_home()?).join("uploads"))
    }

    //------------------------------------------------------------------
    // 대화의 업로드 폴더 지우기 — 모델이 내려가 있어도 지워진다(평범한 파일). 못 지우면 다음 시작 때 정리
    //------------------------------------------------------------------
    fn delete_uploads(&self, conv: &str) {
        if let (Some(root), Some(sid)) = (self.upload_root(), upload_safe_id(conv)) {
            let d = root.join(sid);
            if d.is_dir() {
                match std::fs::remove_dir_all(&d) {
                    Ok(_) => crate::info!("panel", "업로드 문서를 지웠다: {}", d.display()),
                    Err(e) => crate::warn!("panel", "업로드 폴더를 지우지 못했다(다음 시작 때 다시): {} ({})", d.display(), e),
                }
            }
        }
    }

    //------------------------------------------------------------------
    // 주인 없는 업로드 폴더 정리 — 기록에 없는 대화 id 폴더(시작할 때 · 기록이 바뀔 때)
    //------------------------------------------------------------------
    fn gc_uploads(&self) {
        let root = match self.upload_root() {
            Some(r) if r.is_dir() => r,
            _ => return,
        };
        let keep: std::collections::HashSet<String> = self.history.iter()
            .filter_map(|c| c["id"].as_str().and_then(upload_safe_id)).collect();
        if let Ok(rd) = std::fs::read_dir(&root) {
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                if e.path().is_dir() && !keep.contains(&name) {
                    let _ = std::fs::remove_dir_all(e.path());
                    crate::info!("panel", "주인 없는 업로드 폴더를 지웠다: {}", name);
                }
            }
        }
    }

    //------------------------------------------------------------------
    // [+] — 파일 고르기 · 한도 확인 · 워커에 /upload 맡기기 (문서 업로드 설계서 §4·§10)
    //   conv = 패널이 정한 대화 id · have = 이미 올린 문서 수
    //------------------------------------------------------------------
    fn upload_pick(&mut self, conv: &str, have: usize) {
        let w = match &self.worker {
            Some(w) => w.clone(),
            None => {
                self.panel.call("uploadQueued", json!([]));
                self.panel.show_error(&self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into()));
                return;
            }
        };
        if w.availability() == "up" && !w.knows("upload") {
            self.panel.call("uploadQueued", json!([]));
            self.panel.set_state("이 SimpleRAG 는 문서 업로드를 모릅니다 — 새로 빌드한 simplerag 가 필요합니다");
            return;
        }
        let picked = rfd::FileDialog::new().set_title("질문할 문서 고르기 (여러 개 가능)")
            .add_filter("문서", UPLOAD_EXTS).pick_files().unwrap_or_default();
        let mut list = vec![];
        let mut n = have;
        for p in picked {
            let path = p.to_string_lossy().into_owned();
            let name = p.file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
            let ext = p.extension().map(|s| s.to_string_lossy().to_lowercase()).unwrap_or_default();
            let size = std::fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
            let why = if !UPLOAD_EXTS.contains(&ext.as_str()) {
                Some("지원하지 않는 형식입니다".to_string())
            } else if size > UPLOAD_MAX_MB * 1024 * 1024 {
                Some(format!("{}MB 를 넘습니다 — 인덱싱 폴더에 넣어 쓰세요", UPLOAD_MAX_MB))
            } else if n >= UPLOAD_MAX_FILES {
                Some(format!("대화 하나에 {}개까지 올릴 수 있습니다", UPLOAD_MAX_FILES))
            } else {
                None
            };
            if let Some(why) = why {
                list.push(json!({"name": name, "ok": false, "why": why}));
                continue;
            }
            n += 1;
            list.push(json!({"name": name, "ok": true}));
            // 워커가 내려가 있으면 올린다 — 명령은 준비된 뒤 차례로 보내진다(질문이 언제나 먼저)
            w.start();
            w.ensure_started();
            let done = self.up_done.clone();
            let nm = name.clone();
            // ASCII JSON — cp949 로 못 쓰는 글자가 든 파일 이름도 깨지지 않게
            w.command(format!("/upload {}", worker::ascii_json(&json!({"conv": conv, "path": path}))),
                Box::new(move |mut r: Value| {
                    if r.get("name").is_none() {
                        r["name"] = json!(nm);
                    }
                    done.lock().unwrap().push(r);
                }));
            self.up_running += 1;
            crate::info!("panel", "업로드 맡김: {} (대화 {})", name, conv);
        }
        self.panel.call("uploadQueued", Value::Array(list));
    }

    //------------------------------------------------------------------
    // 첨부 칩 [×] — 워커에 /upload-remove (모델이 내려가 있으면 올린 뒤 처리)
    //------------------------------------------------------------------
    fn upload_remove(&mut self, conv: &str, name: &str) {
        if let Some(w) = &self.worker {
            w.start();
            w.ensure_started();
            let done = self.up_done.clone();
            w.command(format!("/upload-remove {}", worker::ascii_json(&json!({"conv": conv, "name": name}))), Box::new(move |r: Value| done.lock().unwrap().push(r)));
            self.up_running += 1;
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
            UserEvent::BrowseIpc(s) => self.on_browse_ipc(&s),
            UserEvent::IconNotes(notes) => {
                self.icon_msg = notes.join("\n");
                if self.info.visible {
                    self.refresh_info();
                }
            }
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
            // AI 답은 물은 화면으로(탐색기 창의 "AI에게 묻기" 면 그 창)
            WEvent::Evidence(items, _ms) => self.answer_call("evidence", Value::Array(items)),
            WEvent::Token(t) => self.answer_call("token", json!(t)),
            WEvent::Done(info) => self.answer_call("finish", info),
            WEvent::Raw(t) => self.answer_call("raw", json!(t)),
            WEvent::Error(m) => self.on_error(&m),
            WEvent::Note(k, m) => self.on_note(&k, &m),
        }
    }

    // 답 사건을 물은 화면으로
    fn answer_call(&mut self, func: &str, arg: Value) {
        if self.ask_to_browse {
            self.browse.call(func, arg);
        } else {
            self.panel.call(func, arg);
        }
    }

    fn on_error(&mut self, msg: &str) {
        crate::warn!("app", "오류: {}", msg);
        if self.ask_to_browse && self.browse.visible {
            self.browse.call("error", json!(msg));
        } else if self.panel.visible {
            self.panel.show_error(msg);
        } else {
            self.tray.notify("MoAI-S", msg);
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
                self.tray.notify("MoAI-S", msg);
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
            _ => self.app_dir(),
        }
    }

    //------------------------------------------------------------------
    // 워커 프로그램 폴더(config.yaml · models 가 있는 곳) — 워커 exe 옆
    //------------------------------------------------------------------
    fn app_dir(&self) -> Option<String> {
        Some(std::path::Path::new(self.cmd.last()?).parent()?.to_string_lossy().into_owned())
    }

    //------------------------------------------------------------------
    // 설정 창 [정밀 모드]·[이어서 묻기] 체크 — config.yaml 의 generation 한 줄을 바꾸고 모델을 다시 올린다
    //   (Python _set_flag 와 같다). 워커는 시작할 때만 config.yaml 을 읽는다.
    //   올라가 있으면 내렸다 다시 올리고, 내려가 있으면 다음 질문 때 새 설정으로 올라간다
    //   key = precision | multiturn · label = 안내 글의 이름 · wait = 다시 올리는 데 걸리는 시간 안내
    //------------------------------------------------------------------
    fn set_flag(&mut self, key: &str, label: &str, on: bool, wait: &str) -> String {
        let (home, app) = (self.data_home(), self.app_dir());
        let cfgp = precision::config_path(home.as_deref(), app.as_deref());
        let mut msg = match precision::save(cfgp.as_deref(), on, key, label) {
            Ok(m) => m,
            Err(e) => return e,
        };
        crate::info!("app", "{} {}: {}", label, if on { "켬" } else { "끔" }, msg);
        if let Some(w) = &self.worker {
            if w.availability() != "down" {
                w.unload(&format!("{} 변경", label));
                w.start();
                w.ensure_started();
                msg += &format!(" — 모델을 다시 올리는 중입니다 ({})", wait);
            } else {
                msg += " — 다음 질문 때 새 설정으로 모델을 올립니다";
            }
        }
        msg
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
                if self.panel_mode == "explorer" {
                    self.push_roots();
                }
            }
            "ask" => {
                self.ask_to_browse = false;
                let q = match one_line(m["text"].as_str().unwrap_or("")) {
                    Some(q) => q,
                    None => return,
                };
                let folder = self.search_folder(self.panel.folder.clone().as_deref());
                // 같은 대화의 바로 앞 질문(멀티턴) — 워커 설정(generation.multiturn)이 켜져 있으면 검색에 이어 붙인다
                let prev = m["prev"].as_str().and_then(one_line);
                // 대화 id · 올린 문서가 있는 대화인가 — 있으면 워커는 그 문서에서만 찾는다(문서 업로드)
                let conv = m["conv"].as_str().map(|s| s.to_string());
                let uploads = m["uploads"].as_bool().unwrap_or(false);
                crate::info!("app", "패널 질문: {:?} (범위 {:?}{})", q.chars().take(40).collect::<String>(), folder,
                             if uploads { " · 업로드 문서" } else { "" });
                match &self.worker {
                    Some(w) => w.ask(&q, folder, prev, conv, uploads),
                    None => {
                        let e = self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into());
                        self.panel.show_error(&e);
                    }
                }
            }
            "upload_pick" => {
                let conv = m["conv"].as_str().unwrap_or("").to_string();
                self.upload_pick(&conv, m["have"].as_u64().unwrap_or(0) as usize);
            }
            "upload_remove" => {
                let (conv, name) = (m["conv"].as_str().unwrap_or("").to_string(), m["name"].as_str().unwrap_or("").to_string());
                self.upload_remove(&conv, &name);
            }
            "delete_conv" => {
                // 대화 한 건 지우기 — 기록에서 빼고 업로드 폴더도 지운다(문서 업로드 설계서 §9)
                let id = m["id"].as_str().unwrap_or("").to_string();
                self.history.retain(|c| c["id"].as_str() != Some(id.as_str()));
                history::save(&self.history_path, &self.history);
                self.delete_uploads(&id);
                let h = self.history_list();
                self.panel.call("setHistory", h);
                crate::info!("panel", "대화 한 건을 지웠다: {}", id);
            }
            "save" => {
                let mut conv = m["conv"].clone();
                let before: Vec<String> = self.history.iter().filter_map(|c| c["id"].as_str().map(String::from)).collect();
                history::touch(&mut self.history, &mut conv);
                // 10개 한도로 밀려난 대화의 업로드 폴더도 지운다
                for id in before.iter().filter(|id| !self.history.iter().any(|c| c["id"].as_str() == Some(id.as_str()))) {
                    self.delete_uploads(id);
                }
                if !history::save(&self.history_path, &self.history) {
                    crate::warn!("panel", "대화 기록을 저장하지 못했다: {}", self.history_path);
                }
                let h = self.history_list();
                self.panel.call("setHistory", h);
            }
            "clear_history" => {
                self.history.clear();
                history::save(&self.history_path, &self.history);
                // 올린 문서도 모두 지운다
                if let Some(root) = self.upload_root() {
                    let _ = std::fs::remove_dir_all(root);
                }
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
            // 탐색기 모드 요청(찾기 · 쪽 열기 · 크게 보기 · 모드 바꾸기 …)
            _ => {
                self.on_find_ipc(false, &m);
            }
        }
    }

    //------------------------------------------------------------------
    // 트레이 메뉴
    //------------------------------------------------------------------
    fn on_tray(&mut self, name: &str) {
        crate::info!("app", "트레이: {}", name);
        match name {
            // 화면 열기 — 탐색기 모드면 MoAI 탐색기 창, RAG 모드면 지금처럼 탐색기 옆 패널
            "open" if self.s.ui_mode == "explorer" => self.open_browse(None, None, Value::Null),
            "open" => self.open_panel(),
            "index_now" | "autoindex" => self.on_tray_autoindex(name),
            "unload" => {
                if let Some(w) = &self.worker {
                    w.unload("트레이 메뉴");
                    self.tray.notify("MoAI-S", "모델을 내렸습니다 — 이제 index 를 실행할 수 있습니다");
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
            None => self.tray.notify("MoAI-S", SCOPE_WARN),
            Some(t) => {
                crate::info!("app", "화면 열기: 범위 폴더를 탐색기로 연다 — {}", t);
                if win::open_explorer(&t).is_err() {
                    self.tray.notify("MoAI-S", &format!("탐색기를 열지 못했습니다: {}", t));
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
        self.tray.notify("MoAI-S", msg);
        self.update_tooltip();
    }

    fn on_tray_autoindex(&mut self, name: &str) {
        if self.jobs.is_none() {
            self.tray.notify("MoAI-S", "자동 인덱싱할 폴더가 없습니다 — [Scope] Folders 를 지정하세요");
            return;
        }
        if name == "index_now" {
            let n = self.jobs.as_mut().unwrap().run_now();
            self.tray.notify("MoAI-S", &format!("폴더 {}개를 인덱싱합니다", n));
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
            self.tray.notify("MoAI-S", &format!("자동 인덱싱을 {}", if on { "켰습니다" } else { "껐습니다" }));
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
        let tip = format!("MoAI-S — {}", text);
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
        self.tray.set_title(&format!("MoAI-S — {}", label));
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
            // 올리는 중이면 경과 초·진행 글 — 설정 창이 진행 막대를 보인다
            info["starting"] = w.starting().unwrap_or(Value::Null);
            if up {
                for (k, v) in w.info().as_object().unwrap() {
                    info[k] = v.clone();
                }
            }
        }
        info["autorun"] = json!(win::autorun_value().is_some());
        // 정밀 모드 — config.yaml 을 못 찾으면 null(체크를 잠근다)
        let (home, app) = (self.data_home(), self.app_dir());
        let cfgp = precision::config_path(home.as_deref(), app.as_deref());
        info["precision"] = cfgp.as_deref().map(|p| json!(precision::read(p, "precision"))).unwrap_or(Value::Null);
        // 멀티턴 — 줄이 없으면 기본 켬
        info["multiturn"] = cfgp.as_deref().map(|p| json!(precision::read(p, "multiturn"))).unwrap_or(Value::Null);
        info["multiturn_note"] = json!([precision::MULTITURN_NOTE, self.info.mt_msg.as_str()].iter()
            .filter(|s| !s.is_empty()).cloned().collect::<Vec<_>>().join("\n"));
        info["precision_note"] = json!(precision::note(precision::model_present(home.as_deref(), app.as_deref()),
                                                       cfgp.is_some(), &self.info.prec_msg));
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
        info["folder_msg"] = json!([self.info.folder_msg.as_str(), self.icon_msg.as_str()].iter()
            .filter(|s| !s.is_empty()).cloned().collect::<Vec<_>>().join("\n"));
        info["ui_mode"] = json!(self.s.ui_mode);
        info["mode_msg"] = json!(self.mode_msg);
        info["page_click"] = json!(self.s.page_click);
        info["folder_icon"] = json!(self.s.folder_icon);
        // 동의어 칸 — 후보 만들기가 끝났으면 그 소식을 결과 글로 옮긴다
        if let Some(m) = self.info.syn_done.lock().unwrap().take() {
            self.info.syn_msg = m;
        }
        if let Some(home) = self.data_home() {
            let (rows, n_left) = synonyms::pending(&home, synonyms::SHOW_ROWS);
            let (n_miss, n_ok) = synonyms::counts(&home);
            info["syn"] = json!({"rows": rows, "n_left": n_left, "approved": synonyms::approved_pairs(&home),
                                 "note": synonyms::note(n_miss, n_left, n_ok, &self.info.syn_msg)});
            // 사람 탭 — meta people 명령이 끝났으면 그 소식을 결과 글로 옮긴다
            if let Some(m) = self.info.ppl_done.lock().unwrap().take() {
                self.info.ppl_msg = m;
            }
            info["people"] = people::info(&home, &self.info.ppl_msg);
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

    //------------------------------------------------------------------
    // 설정 창 사람 탭 — "simplerag-rs meta people …" 를 뒤에서 돌린다 (메타데이터 검색 설계서 §7-2 · 6단계)
    //=> people.yaml 쓰기와 다시 정규화(문서 2천여 개에 1초 안팎)는 SimpleRAG 가 한다 — 글 모양 · 규칙을 한 곳에.
    //   meta.sqlite 만 고치고 벡터 인덱스는 건드리지 않으므로 워커가 떠 있어도 함께 돌려도 된다.
    //   끝나면 명령의 첫 줄을 소식으로 남긴다(다음 갱신 때 사람 탭 안내 줄에 보인다).
    //
    // -in: args = meta people 뒤 인자들(예: ["approve", "bong9", "고봉수"])
    // -out: 지금 보일 안내 글
    //------------------------------------------------------------------
    fn people_cmd(&mut self, args: Vec<String>) -> String {
        use std::sync::atomic::Ordering;
        if self.cmd.is_empty() {
            return "SimpleRAG 를 찾지 못했습니다".into();
        }
        if self.info.ppl_running.load(Ordering::SeqCst) {
            return "앞 명령을 처리하는 중입니다 — 잠시 뒤 다시 누르세요".into();
        }
        self.info.ppl_running.store(true, Ordering::SeqCst);
        let mut cmd = self.cmd.clone();
        cmd.push("meta".into());
        cmd.push("people".into());
        cmd.extend(args.iter().cloned());
        crate::info!("app", "사람 사전: {}", args.join(" "));
        let (done, running) = (self.info.ppl_done.clone(), self.info.ppl_running.clone());
        let suggest = args.first().map(|a| a == "suggest").unwrap_or(false);
        std::thread::spawn(move || {
            let mut c = std::process::Command::new(&cmd[0]);
            c.args(&cmd[1..]).stdin(std::process::Stdio::null());
            if let Some(d) = std::path::Path::new(&cmd[0]).parent() {
                c.current_dir(d);
            }
            {
                use std::os::windows::process::CommandExt;
                c.creation_flags(0x0800_0000);
            }
            let msg = match c.output() {
                Ok(o) => {
                    let out = String::from_utf8_lossy(&o.stdout).into_owned();
                    let first = out.lines().map(|l| l.trim()).find(|l| !l.is_empty()).unwrap_or("").to_string();
                    if !o.status.success() {
                        let err = String::from_utf8_lossy(&o.stderr).lines().last().unwrap_or("").trim().to_string();
                        format!("처리하지 못했습니다: {}", if first.is_empty() { err } else { first })
                    } else if suggest {
                        // "별칭 후보 N개 (점수 순 …)" → 짧게
                        format!("{} — 아래에서 고르세요", first.split(" (").next().unwrap_or(&first))
                    } else {
                        first
                    }
                }
                Err(e) => format!("SimpleRAG 를 실행하지 못했습니다: {}", e),
            };
            crate::info!("app", "사람 사전 결과: {}", msg);
            *done.lock().unwrap() = Some(msg);
            running.store(false, Ordering::SeqCst);
        });
        if suggest { "별칭 후보를 찾는 중입니다…".into() } else { "적는 중입니다…".into() }
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
            "precision" => self.info.prec_msg = self.set_flag("precision", "정밀 모드", m["on"].as_bool().unwrap_or(false),
                "4B 는 20초 안팎 · 이 PC 에서 4B 를 처음 올리면 CPU·iGPU 측정으로 1~2분"),
            "multiturn" => self.info.mt_msg = self.set_flag("multiturn", "이어서 묻기", m["on"].as_bool().unwrap_or(true), "10초 안팎"),
            "add_folder" => {
                let msg = self.add_folder_dialog();
                if !msg.is_empty() {
                    self.info.folder_msg = msg;
                }
            }
            "remove_folder" => {
                let target = m["folder"].as_str().unwrap_or("").to_string();
                let v: Vec<String> = self.current_folders().into_iter().filter(|f| *f != target).collect();
                self.info.folder_msg = self.set_folders(v).unwrap_or_else(|e| e);
            }
            "folder_msg" => self.info.folder_msg = m["msg"].as_str().unwrap_or("").to_string(),
            // 화면 모드(탐색기 · AI) — ini 에 남기고 패널 화면을 바로 바꾼다(다시 시작 없이)
            "ui_mode" => {
                let mode = if m["value"] == "rag" { "rag" } else { "explorer" };
                self.save_ui_mode(mode);
                self.set_panel_mode(mode);
            }
            "page_click" => {
                let v = if m["value"] == "app" { "app" } else { "preview" };
                self.s.page_click = v.to_string();
                if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "PageClick", v) {
                    self.info.action_msg = e;
                }
                self.push_roots();
            }
            "folder_icon" => {
                let on = m["on"].as_bool().unwrap_or(true);
                self.s.folder_icon = on;
                self.icon_msg = if on { "인덱싱 폴더 아이콘을 바꾸는 중…".into() } else { "아이콘을 원래대로 돌리는 중…".into() };
                if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "FolderIcon", if on { "1" } else { "0" }) {
                    self.icon_msg = e;
                }
                self.sync_icons();
            }
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
            // 사람 탭 — [맞음] · [아님] · [빼기] · [나 저장] · [후보 만들기]
            "ppl_approve" | "ppl_reject" | "ppl_remove" | "ppl_me" | "ppl_suggest" => {
                let a = |k: &str| m[k].as_str().unwrap_or("").trim().to_string();
                let args: Vec<String> = match m["type"].as_str().unwrap_or("") {
                    "ppl_approve" => vec!["approve".into(), a("alias"), a("name")],
                    "ppl_reject" => vec!["reject".into(), a("alias"), a("name")],
                    "ppl_remove" => vec!["remove".into(), a("alias")],
                    "ppl_me" => vec!["me".into(), a("name")],
                    _ => vec!["suggest".into()],
                };
                self.info.ppl_msg = if args.iter().skip(1).any(|x| x.is_empty()) {
                    "별칭과 이름을 모두 적으세요".into()
                } else {
                    self.people_cmd(args)
                };
            }
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
        // 폴더 아이콘 — 새 폴더는 붙이고 뺀 폴더는 되돌린다 · 탐색 창 폴더 목록도 새로
        self.sync_icons();
        self.push_roots();
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
        self.browse.hide();
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
            rfd::MessageDialog::new().set_title("MoAI-S").set_description(text).show();
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
    // --folder-icons restore — 바꾼 폴더 · 드라이브 아이콘을 모두 원래대로(앱을 지우기 전에)
    if args.first().map(|a| a == "--folder-icons").unwrap_or(false) {
        let notes = folder_icon::sync(&[], false);
        let msg = if notes.is_empty() { "폴더 · 드라이브 아이콘을 모두 원래대로 돌렸습니다".to_string() } else { notes.join("; ") };
        println!("{}", msg);
        std::process::exit(if notes.is_empty() { 0 } else { 1 });
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
    let pwin = WindowBuilder::new().with_title("MoAI-S").with_visible(false).with_window_icon(icon.clone())
        .with_inner_size(tao::dpi::PhysicalSize::new(s.win_width as u32, 700)).build(&event_loop).expect("패널 창");
    let font = font_px(s.font_size);
    let moai = {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD.encode(MOAI_PNG)
    };
    // 패널 화면 — 탐색기 모드(기본)면 browse.html 좁은 배치, RAG 모드면 대화 화면
    let html = panel_html(&s.ui_mode, &s, &moai);
    let p1 = proxy.clone();
    let pview = WebViewBuilder::with_web_context(ctx).with_html(html)
        .with_ipc_handler(move |req| post(&p1, UserEvent::PanelIpc(req.into_body())))
        .build(&pwin).expect("패널 화면");

    // 설정 창
    let iwin = WindowBuilder::new().with_title("MoAI-S 설정").with_visible(false).with_window_icon(icon)
        .with_resizable(false).with_inner_size(tao::dpi::LogicalSize::new(760.0, 800.0)).build(&event_loop).expect("설정 창");
    let p2 = proxy.clone();
    let iview = WebViewBuilder::with_web_context(unsafe { &mut *(ctx as *mut WebContext) })
        .with_html(INFO_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", &moai))
        .with_ipc_handler(move |req| post(&p2, UserEvent::InfoIpc(req.into_body())))
        .build(&iwin).expect("설정 화면");

    // MoAI 탐색기 창 — 목록 화면 + 그 위의 PDF 미리 보기 WebView(자리는 화면이 알려 준다)
    let bwin = WindowBuilder::new().with_title("MoAI 탐색기").with_visible(false).with_window_icon(Icon::from_resource(1, None).ok())
        .with_inner_size(tao::dpi::LogicalSize::new(1240.0, 780.0)).with_min_inner_size(tao::dpi::LogicalSize::new(760.0, 420.0))
        .build(&event_loop).expect("탐색기 창");
    let p3 = proxy.clone();
    let bview = WebViewBuilder::with_web_context(unsafe { &mut *(ctx as *mut WebContext) })
        .with_html(BROWSE_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", &moai).replace("__LAYOUT__", "window"))
        .with_ipc_handler(move |req| post(&p3, UserEvent::BrowseIpc(req.into_body())))
        .build(&bwin).expect("탐색기 화면");
    let pv = WebViewBuilder::with_web_context(unsafe { &mut *(ctx as *mut WebContext) })
        .with_url("about:blank").with_visible(false)
        .with_bounds(wry::Rect { position: wry::dpi::LogicalPosition::new(0.0, 0.0).into(), size: wry::dpi::LogicalSize::new(1.0, 1.0).into() })
        .build_as_child(&bwin);
    let pv = match pv {
        Ok(v) => Some(v),
        Err(e) => {
            crate::warn!("browse", "미리 보기 WebView 를 만들지 못했다 — 글 미리 보기만 쓴다: {}", e);
            None
        }
    };

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
                       syn_msg: String::new(), syn_done: Arc::new(Mutex::new(None)), syn_running: Arc::new(std::sync::atomic::AtomicBool::new(false)),
                       ppl_msg: String::new(), ppl_done: Arc::new(Mutex::new(None)), ppl_running: Arc::new(std::sync::atomic::AtomicBool::new(false)),
                       prec_msg: String::new(), mt_msg: String::new() },
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
        up_done: Arc::new(Mutex::new(vec![])),
        up_running: 0,
        browse: Browse::new(bwin, bview, pv),
        panel_mode: String::new(),
        ask_to_browse: false,
        find_done: Arc::new(Mutex::new(vec![])),
        icon_msg: String::new(),
        mode_msg: String::new(),
        moai_b64: moai,
    };
    app.panel_mode = app.s.ui_mode.clone();
    app.jobs = app.make_jobs();
    app.start();

    let panel_id = app.panel.window.id();
    let info_id = app.info.window.id();
    let browse_id = app.browse.window.id();
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
                } else if window_id == browse_id {
                    // 닫기 = 숨기기(트레이에 남는다)
                    app.browse.hide();
                }
            }
            _ => {}
        }
        if app.quitting {
            *flow = ControlFlow::Exit;
        }
    });
}
