//------------------------------------------------------------------
// MoAISearch(RAGSearchBox Rust 판) — 트레이 상주 앱 진입점 (Python main.py · app.py)
//=> 탐색기에서 지정 폴더를 열면 오른쪽에 대화 패널이 붙고, 질문은 상주 워커(simplerag-rs chat)가 답한다.
//   구성: 트레이(자기 스레드) · 탐색기 감시(자기 스레드) · 워커 관리(자기 스레드) · 폴더 감시(폴더마다 스레드)
//         · 패널/설정 창(메인 스레드, WebView2). 다른 스레드의 일은 모두 사용자 이벤트로 메인에 넘어온다.
//------------------------------------------------------------------
#![windows_subsystem = "windows"]

mod browse_win;
mod candidates;
mod explorer;
mod folder_check;
mod folder_icon;
mod history;
mod jobs;
mod log;
mod logview;
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
// 사용 설명서 파일 이름(설치 폴더 doc 안) · 사용 안내 판 — 안내 단계가 크게 바뀌면 올려 다시 보여 준다
const MANUAL_NAME: &str = "MoAISearch-사용자-설명서.html";
const TOUR_VERSION: u32 = 1;
// 설정 창 크기(논리 픽셀) — 작은 화면에서는 작업 영역에 맞춰 줄인다(show_info)
const INFO_W: f64 = 720.0;
const INFO_H: f64 = 720.0;
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
    // 패널에 파일을 끌어 놓음(들어옴 · 놓음 · 나감) — 채팅 모드 문서 올리기
    PanelDrop(wry::DragDropEvent),
    // MoAI 탐색기 창에 파일을 끌어 놓음 — 비슷한 문서 찾기 입력란
    BrowseDrop(wry::DragDropEvent),
    // 폴더 아이콘 맞추기가 끝났다 — 실패 안내 목록(성공이면 비어 있음)
    IconNotes(Vec<String>),
    // 설정 > 로그 판 — 스레드가 모은 실패 문서 · 로그 경고·오류
    LogView(Value),
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
    // 마지막 인덱싱 확장자 바꾸기 결과
    ext_msg: String,
}

//------------------------------------------------------------------
// 앱 — 모든 구성요소
//------------------------------------------------------------------
// 문서 업로드 한도(문서 업로드 설계서 §10) · 올릴 수 있는 확장자(SimpleRAG indexer TEXT_EXTS 와 같다)
const UPLOAD_MAX_FILES: usize = 5;
const UPLOAD_MAX_MB: u64 = 30;
const UPLOAD_EXTS: &[&str] = &["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "hwp", "hwpx", "txt", "md", "csv", "html", "htm", "py", "ipynb"];

//------------------------------------------------------------------
// 대화 id → 업로드 폴더 이름 (SimpleRAG uploads::safe_id 와 같은 규칙)
//------------------------------------------------------------------
fn upload_safe_id(conv: &str) -> Option<String> {
    let s: String = conv.chars().map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-' { c } else { '_' }).collect();
    let s = s.trim_matches(|c| c == '.' || c == '_').to_string();
    if s.is_empty() { None } else { Some(s) }
}

//------------------------------------------------------------------
// 비슷한 문서 찾기에 쓸 수 없는 이유 — 쓸 수 있으면 None ([+] · 끌어 놓기 공통, 설계서 §4-3)
//=> 인덱스에 있는 문서는 벡터를 다시 만들지 않으므로 업로드 크기 한도는 두지 않는다.
//
// -in: p = 고르거나 놓은 경로
//
// -out: 거절 이유 / 쓸 수 있으면 None
// -out: error = 없음
//------------------------------------------------------------------
fn similar_reject(p: &std::path::Path) -> Option<String> {
    if p.is_dir() {
        return Some("폴더가 아닌 문서 파일을 넣어 주세요".into());
    }
    let ext = p.extension().map(|s| s.to_string_lossy().to_lowercase()).unwrap_or_default();
    if !UPLOAD_EXTS.contains(&ext.as_str()) {
        return Some(format!("지원하지 않는 형식입니다 — 지원 형식: {}", UPLOAD_EXTS.join(", ")));
    }
    // 오피스 잠금 파일(~$문서.docx)은 내용이 없다
    if p.file_name().map(|n| n.to_string_lossy().starts_with("~$")).unwrap_or(false) {
        return Some("임시 파일입니다".into());
    }
    None
}

// JSON 글자 배열 → Vec<String> (배열이 아니거나 글자가 아닌 칸은 건너뛴다)
fn str_list(v: &Value) -> Vec<String> {
    v.as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or_default()
}

//------------------------------------------------------------------
// 올릴 수 없는 이유 — 올릴 수 있으면 None ([+] · 끌어 놓기 공통 검사, 채팅 첨부 설계서 §2-4)
//   is_dir = 폴더인가 · ext = 소문자 확장자 · size = 바이트 · have = 이미 받아 둔 수 · dup = 같은 이름이 이미 있나
//------------------------------------------------------------------
fn upload_reject(is_dir: bool, ext: &str, size: u64, have: usize, dup: bool) -> Option<String> {
    if is_dir {
        // 폴더는 문서 묶음이라 업로드(대화당 몇 건) 대신 검색 폴더로 쓰는 게 맞다
        return Some("폴더는 올릴 수 없습니다 — 검색 폴더로 추가하세요".into());
    }
    if !UPLOAD_EXTS.contains(&ext) {
        return Some("지원하지 않는 형식입니다".into());
    }
    if size > UPLOAD_MAX_MB * 1024 * 1024 {
        // 실제 크기를 함께 — 얼마나 넘는지 보이게
        return Some(format!("{:.1}MB — {}MB 를 넘습니다 · 검색 폴더에 넣어 쓰세요", size as f64 / 1048576.0, UPLOAD_MAX_MB));
    }
    if dup {
        return Some("같은 이름의 문서가 이미 있습니다".into());
    }
    if have >= UPLOAD_MAX_FILES {
        return Some(format!("대화 하나에 {}개까지 올릴 수 있습니다", UPLOAD_MAX_FILES));
    }
    None
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
    // 탐색 창 폴더 숫자를 마지막으로 고친 때의 진행 번호 · 시각
    roots_ver: u64,
    t_roots: Instant,
    t_info: Instant,
    // 사전 후보 알림 — 상태 · 1분마다 보기 · 화면을 마지막으로 건드린 때 · 설정 버튼 숫자
    cand: candidates::CandState,
    t_cand: Instant,
    t_user: Instant,
    cand_n: usize,
    quitting: bool,
    selftest_done: bool,
    // 탐색기 모드(탐색기 모드 설계서) — MoAI 탐색기 창 · 패널에 지금 올린 화면(explorer | rag)
    browse: Browse,
    panel_mode: String,
    // 답 · AI 정리를 어느 화면이 요청했나 — true 면 탐색기 창, false 면 패널
    ask_to_browse: bool,
    // /find 결과(워커 스레드가 넣고 pump 가 화면으로 옮긴다) — (탐색기 창으로?, 결과)
    find_done: Arc<Mutex<Vec<(bool, Value)>>>,
    // 폴더 아이콘 맞추기 결과 안내(설정 창 폴더 메모에)
    icon_msg: String,
    // 화면 모드를 바꾼 결과 안내(설정 창 화면 모드 칸에)
    mode_msg: String,
    moai_b64: String,
    // 이번 실행에서 "인덱싱 폴더를 지정해 주세요" 를 이미 물었나 — 취소하면 다음 실행 때 다시 묻는다
    first_folder_asked: bool,
    // 모델 준비 막대를 보이는 중인가 · 마지막으로 보낸 때
    load_shown: bool,
    t_load: Instant,
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
// 사용자에게 보일 오류 글 — 인덱스 잠금 오류는 할 수 있는 일을 알려 주는 말로 바꾼다
//=> 인덱싱 프로세스가 인덱스를 쥐고 있을 때 나는 오류라 끝나면 저절로 풀린다. 원래 글은 로그에 남는다.
//------------------------------------------------------------------
fn plain_error(msg: &str) -> String {
    if msg.contains("already accessed") {
        "문서를 인덱싱하고 있어 잠시 검색할 수 없습니다 — 끝나면 다시 시도해 주세요".into()
    } else {
        msg.to_string()
    }
}

//------------------------------------------------------------------
// 패널 화면 고르기 — 탐색기 모드면 browse.html(좁은 배치), RAG 모드면 대화 화면 panel.html
//------------------------------------------------------------------
fn panel_html(mode: &str, s: &Settings, moai: &str) -> String {
    let font = font_px(s.font_size);
    if mode == "explorer" {
        BROWSE_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", moai).replace("__LAYOUT__", "panel").replace("__UISTATE__", "null")
    } else {
        // 업로드 한도는 상수 한 곳에서 — 화면 안내 글이 숫자를 따로 갖지 않게
        PANEL_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", moai).replace("__SCOPE__", &s.panel_search_scope)
            .replace("__UP_MAX_FILES__", &UPLOAD_MAX_FILES.to_string()).replace("__UP_MAX_MB__", &UPLOAD_MAX_MB.to_string())
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
        let tray_only = std::env::args().any(|a| a == "--tray");
        if !usable {
            crate::warn!("app", "{}", SCOPE_WARN);
            // 직접 실행했고 폴더가 하나도 없으면 곧 안내 창으로 묻는다 — ini 를 고치라는 풍선은 띄우지 않는다
            if tray_only || !self.current_folders().is_empty() {
                self.tray.notify("MoAI-S", SCOPE_WARN);
            }
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
        } else if !tray_only {
            // 직접 실행하면 화면을 바로 띄운다(트레이 "열기" 와 같다) — 로그인 자동 시작(--tray)은 트레이에만
            crate::info!("app", "시작 화면 띄우기 ({} 모드)", self.s.ui_mode);
            // 인덱싱 폴더가 없으면 채팅 모드는 열 폴더가 없다 — 폴더를 고르는 탐색기 창으로 연다
            //   탐색기 모드는 [나란히 보기] 켬이면 채팅 모드처럼 Windows 탐색기 옆 패널로 띄운다
            if self.current_folders().is_empty() || (self.s.ui_mode == "explorer" && !self.s.panel_enabled) {
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
        // 인덱싱 진행이 바뀌면 탐색 창 숫자를 고친다 — 0.5초에 한 번까지(상태 파일을 매번 읽지 않게)
        let ver = self.jobs.as_ref().map(|j| j.prog_ver).unwrap_or(0);
        if ver != self.roots_ver && self.t_roots.elapsed() >= Duration::from_millis(500) {
            self.roots_ver = ver;
            self.t_roots = Instant::now();
            let v = self.roots_value();
            self.browse.call("setRootCounts", v);
            self.push_vec_status();
        }
        if self.t_tooltip.elapsed() >= TOOLTIP_EVERY {
            self.update_tooltip();
        }
        if self.info.visible && self.t_info.elapsed() >= INFO_EVERY {
            self.refresh_info();
        }
        if self.t_load.elapsed() >= Duration::from_millis(500) {
            self.t_load = Instant::now();
            self.push_load();
        }
        self.pump_uploads();
        self.pump_finds();
        if self.t_cand.elapsed() >= Duration::from_secs(60) {
            self.t_cand = Instant::now();
            self.check_candidates();
        }
    }

    //------------------------------------------------------------------
    // 사전 후보 점검(1분마다) — 설정 버튼 숫자, 쉬는 동안 후보 만들기, 새 후보 알림 (candidates.rs)
    //    1) 남은 후보 수를 탐색기 [⚙ 설정] 버튼에 — 알림을 놓쳐도 보이게
    //    2) 화면을 10분 넘게 안 썼고 인덱싱 · 답변 · 후보 만들기 중이 아니면 이름 별칭(하루 한 번),
    //       놓친 질문이 10개 넘게 새로 쌓였으면 동의어 후보를 만든다
    //    3) 새 후보 묶음이면 하루 한 번까지 트레이 알림 — 누르면 설정 창 그 탭
    //------------------------------------------------------------------
    fn check_candidates(&mut self) {
        use std::sync::atomic::Ordering;
        let Some(home) = self.data_home() else { return };
        let (n_syn, n_ppl, sig) = candidates::pending(&home);
        if n_syn + n_ppl != self.cand_n {
            self.cand_n = n_syn + n_ppl;
            self.browse.call("setCandCount", json!(self.cand_n));
        }
        if !self.s.cand_notify {
            return;
        }
        let t = candidates::now();
        let before = self.cand.clone();
        let busy = self.index_running() || self.info.syn_running.load(Ordering::SeqCst) || self.info.ppl_running.load(Ordering::SeqCst)
            || self.worker.as_ref().map(|w| w.availability() == "starting" || (w.availability() == "up" && !w.is_idle())).unwrap_or(false);
        let idle = self.t_user.elapsed() >= Duration::from_secs(candidates::IDLE_SECS) && !busy;
        if idle && candidates::ppl_due(&self.cand, t) {
            crate::info!("app", "사전 후보: 쉬는 동안 이름 별칭 후보를 만든다");
            self.cand.ppl_at = t;
            self.info.ppl_msg = self.people_cmd(vec!["suggest".into()]);
        } else if idle {
            let (n_miss, _) = synonyms::counts(&home);
            if candidates::syn_due(&self.cand, n_miss, t) {
                crate::info!("app", "사전 후보: 놓친 질문 {}개 — 쉬는 동안 동의어 후보를 만든다", n_miss);
                self.cand.syn_at = t;
                self.cand.syn_base = n_miss;
                self.info.syn_msg = self.suggest_synonyms();
            }
        }
        // 설정 창을 보고 있으면 알리지 않는다(이미 보고 있다)
        if !self.info.visible && candidates::should_notify(&self.cand, &sig, t) {
            crate::info!("app", "사전 후보 알림: 동의어 {} · 이름 별칭 {}", n_syn, n_ppl);
            self.tray.notify_action("MoAI-S 사전 후보", &candidates::message(n_syn, n_ppl), Some("candidates"));
            self.cand.last_notify = t;
            self.cand.last_sig = sig;
        }
        if self.cand != before {
            self.save_cand_state();
        }
    }

    // 사전 후보 알림 상태 저장 — INI [UI] CandidateState
    fn save_cand_state(&mut self) {
        let v = self.cand.text();
        if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "CandidateState", &v) {
            crate::warn!("app", "사전 후보 알림 상태를 저장하지 못했다: {}", e);
        }
        self.s.cand_state = v;
    }

    //------------------------------------------------------------------
    // 알림 · [⚙ 설정] 숫자를 눌렀다 — 설정 창을 후보가 있는 탭으로 연다(동의어 먼저)
    //------------------------------------------------------------------
    fn open_candidates(&mut self) {
        self.show_info();
        let tab = match self.data_home() {
            Some(home) if candidates::pending(&home).0 == 0 => "ppl",
            _ => "syn",
        };
        if self.info.ready {
            let _ = self.info.webview.evaluate_script(&format!("showTab('{}')", tab));
        }
    }

    // 탐색기 창 맨 아래 줄의 모델 준비 막대 — 올리는 동안 % 를 보내고, 끝나면 한 번 지운다
    fn push_load(&mut self) {
        let st = self.worker.as_ref().and_then(|w| w.starting());
        if st.is_some() || self.load_shown {
            self.load_shown = st.is_some();
            self.browse.call("setLoad", st.unwrap_or(Value::Null));
        }
    }

    //------------------------------------------------------------------
    // 지금 인덱싱 단계 — 검색 결과에 실어 보낸다. 화면은 결과가 비었을 때 이 값으로 안내 글을 고른다
    //=> 결과가 대조 시작 직전에 올 수 있어, 화면이 가진 진행 값 대신 결과를 보낼 때의 상태를 쓴다
    //------------------------------------------------------------------
    fn index_note(&self) -> Value {
        if self.worker.as_ref().map(|w| w.starting().is_some()).unwrap_or(false) {
            return json!({"stage": "model"});
        }
        match self.jobs.as_ref().and_then(|j| j.index_stage()) {
            Some((n, total)) => json!({"stage": "index", "n": n, "total": total}),
            None => Value::Null,
        }
    }

    // /find 결과를 물은 화면으로
    fn pump_finds(&mut self) {
        let done: Vec<(bool, Value)> = std::mem::take(&mut *self.find_done.lock().unwrap());
        let note = self.index_note();
        for (to_browse, mut r) in done {
            r["indexing"] = note.clone();
            // 화면은 done.error · why 를 그대로 보여 준다 — 잠금 오류는 쉬운 말로 바꿔 넘긴다
            if let Some(e) = r["why"].as_str().map(plain_error) {
                r["why"] = json!(e);
            }
            if let Some(e) = r["done"]["error"].as_str().map(plain_error) {
                r["done"]["error"] = json!(e);
            }
            // 모두 찾기의 나머지 쪽 · 쪽 본문은 따로 받는다
            let func = match r["kind"].as_str() {
                Some("doc") => "docPages",
                Some("page") => "pageText",
                _ => "results",
            };
            if to_browse {
                self.browse.call(func, r);
            } else {
                self.panel.call(func, r);
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
    // 탐색기 창 맨 아래 줄 — 벡터 채우기 진행(없으면 빈 글)
    fn push_vec_status(&mut self) {
        let t = self.jobs.as_ref().and_then(|j| j.pending_text()).unwrap_or_default();
        self.browse.call("setVecStatus", json!(t));
    }

    fn push_roots(&mut self) {
        let roots = self.roots_value();
        self.push_vec_status();
        self.browse.call("setExcluded", json!(self.s.exclude_folders));
        self.browse.call("setRoots", roots.clone());
        self.browse.call("setPageClick", json!(self.s.page_click));
        self.browse.call("setFindAll", json!(self.s.find_all));
        if self.panel_mode == "explorer" {
            self.panel.call("setRoots", roots);
            self.panel.call("setPageClick", json!(self.s.page_click));
        } else {
            self.push_panel_folders();
        }
    }

    //------------------------------------------------------------------
    // 인덱싱 폴더 목록 값 — 경로 · 이름 · 문서 수 + 진행(끝낸 수, 전체) · 허락 대기 새 문서 수
    //------------------------------------------------------------------
    fn roots_value(&mut self) -> Value {
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
        // 인덱싱 중이면 진행, 한도를 넘어 기다리면 대기 수 — 끝나면 문서 수만 남는다
        if let (Some(j), Some(arr)) = (self.jobs.as_ref(), roots.as_array_mut()) {
            for r in arr.iter_mut() {
                let (prog, wait) = j.root_state(r["path"].as_str().unwrap_or(""));
                if let Some((n, total)) = prog {
                    r["progress"] = json!([n, total]);
                }
                if wait > 0 {
                    r["waiting"] = json!(wait);
                }
                if j.root_delete_wait(r["path"].as_str().unwrap_or("")) {
                    r["waiting_del"] = json!(true);
                }
            }
        }
        roots
    }

    //------------------------------------------------------------------
    // MoAI 탐색기 창 열기 — q 가 있으면 그 검색을 바로 돌리고(크게 보기), select 쪽을 고른다
    //------------------------------------------------------------------
    fn open_browse(&mut self, q: Option<&str>, folder: Option<String>, select: Value) {
        self.push_roots();
        self.browse.show();
        // tao 가 창을 보이며 스타일을 다시 쓰므로 📌 항상 위를 다시 건다
        win::set_topmost(self.browse.hwnd(), self.s.browse_on_top);
        if let Some(q) = q.filter(|q| !q.trim().is_empty()) {
            self.browse.js(format!("rsb.runQuery({}, {}, {})", json!(q), json!(folder), select));
        }
        if let Some(w) = &self.worker {
            if self.s.start_mode != "lazy" {
                w.ensure_started();
            }
        }
        self.ask_first_folder();
    }

    //------------------------------------------------------------------
    // 첫 실행 — 인덱싱 폴더가 하나도 없으면 안내 창을 띄우고 폴더 고르기 창으로 넣게 한다
    //=> 탐색기 창이 보이고 화면이 다 뜬 뒤에 묻는다(창은 답할 때까지 멈춘다). 한 실행에 한 번만 묻고,
    //   취소하면 탐색 창의 [＋ 폴더 추가] 로 넣게 안내한다 — 폴더가 없으면 다음 실행 때 다시 묻는다.
    //------------------------------------------------------------------
    fn ask_first_folder(&mut self) {
        if self.first_folder_asked || !self.browse.visible || !self.browse.is_ready() || !self.current_folders().is_empty() {
            return;
        }
        self.first_folder_asked = true;
        // 알림 창 대신 탐색기 창 안의 환영 화면 — [검색 폴더 고르기] 를 누르면 add_folder (첫 실행 안내 설계서 §4)
        crate::info!("app", "인덱싱 폴더가 없어 환영 화면을 보인다");
        self.browse.call("setFirstRun", json!(true));
        self.browse.call("notice", json!("찾을 문서가 든 폴더를 고르세요"));
    }

    //------------------------------------------------------------------
    // 사용 설명서 경로 — 설치 폴더(MoAISearch.exe 옆)의 doc\MoAISearch-사용자-설명서.html, 없으면 None
    //------------------------------------------------------------------
    fn manual_path() -> Option<String> {
        let p = std::env::current_exe().ok()?.parent()?.join("doc").join(MANUAL_NAME);
        p.is_file().then(|| p.to_string_lossy().into_owned())
    }

    // 사용 안내를 본 것으로 기록 — INI [UI] TourDone (안내 판)
    fn mark_tour_done(&mut self) {
        if self.s.tour_done >= TOUR_VERSION {
            return;
        }
        self.s.tour_done = TOUR_VERSION;
        if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "TourDone", &TOUR_VERSION.to_string()) {
            crate::warn!("app", "사용 안내 기록을 저장하지 못했다: {}", e);
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
    //    · 채팅 모드: 탐색기 창을 숨기고 고른 폴더(없으면 맨 처음 인덱싱 폴더)를 탐색기로 열어 옆에 대화 패널
    //    · 탐색기 모드: 대화 패널을 숨기고 MoAI 탐색기 창
    //   folder = 탐색 창에서 고른 폴더
    //------------------------------------------------------------------
    fn switch_ui_mode(&mut self, mode: &str, folder: Option<String>) {
        crate::info!("app", "화면 모드 바꾸기: {}", mode);
        self.save_ui_mode(mode);
        self.set_panel_mode(mode);
        if mode == "rag" {
            self.browse.hide();
            self.open_panel_at(folder);
        } else if self.s.panel_enabled && !self.current_folders().is_empty() {
            // [나란히 보기] 켬 — MoAI 탐색기 창이 아니라 같은 패널이 탐색기 모드 화면으로 바뀐다(탐색기 옆 팝업)
            if self.panel.docked() {
                self.panel.activate();
            } else {
                // 따로 떠 있던 패널(탐색기를 닫아 남은 것) · 숨은 패널 → 폴더를 탐색기로 열어 옆에 붙인다
                let f = folder.or_else(|| self.panel.folder.clone());
                self.panel.hide(false);
                self.browse.hide();
                self.open_panel_at(f);
            }
        } else {
            self.panel.hide(true);
            self.open_browse(None, None, Value::Null);
        }
    }

    //------------------------------------------------------------------
    // 인덱싱 폴더 추가 — 폴더 고르기 창을 띄워 넣는다. 돌려줌: 안내 글(취소하면 빈 글)
    //------------------------------------------------------------------
    fn add_folder_dialog(&mut self) -> String {
        let p = match rfd::FileDialog::new().set_title("검색할 폴더 고르기").pick_folder() {
            Some(p) => p.to_string_lossy().into_owned(),
            None => return String::new(),
        };
        // 드라이브 전체 · 네트워크 · 클라우드 · 시스템 폴더는 넣지 않는다
        if let Some(why) = folder_check::refusal(&p) {
            crate::info!("app", "폴더 추가 거절: {} — {}", p, why);
            return why;
        }
        let cur = self.current_folders();
        if cur.iter().any(|f| f.to_lowercase() == p.to_lowercase()) {
            return "이미 들어 있는 폴더입니다".into();
        }
        let first = cur.is_empty();
        let mut v = cur;
        v.push(p.clone());
        let mut added = false;
        let msg = match self.set_folders(v) {
            Ok(m) => {
                // 직접 고른 폴더는 새 문서가 많아도 묻지 않고 바로 인덱싱한다
                if let Some(j) = self.jobs.as_mut() {
                    j.approve_folder(&p, "폴더 추가");
                }
                added = true;
                m
            }
            Err(e) => e,
        };
        self.push_roots();
        // 첫 폴더 — 환영 화면을 거두고, 아직 안 봤으면 인덱싱이 도는 동안 사용 안내(설계서 §3)
        if added && first {
            self.browse.call("setFirstRun", json!(false));
            if self.s.tour_done < TOUR_VERSION {
                crate::info!("app", "첫 폴더를 넣었다 — 사용 안내를 시작한다");
                self.browse.call("startTour", Value::Null);
            }
        }
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
    //   find · similar · similar_pick · preview · preview_hide · pv_rect · open_page · reveal · list_dirs · brief · brief_cancel · big · mode
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
                let mut waits = false;
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
                        // 찾는 동안 · 찾은 뒤 잠깐은 벡터 채우기가 쉰다 — 연속으로 찾을 때 기다리지 않게
                        w.note_use();
                        waits = w.is_indexing();
                        // 조건 칩에서 끈 조건 · 넓힌 기간(메타데이터 검색 설계서 §14)을 함께 넘긴다
                        let req = json!({"q": q, "folder": folder, "off": m["off"].as_array().cloned().unwrap_or_default(),
                                         "widen": m["widen"].as_i64().unwrap_or(0),
                                         // 모두 찾기 — 그것을 아는 워커에게만(옛 워커는 지금처럼 찾는다)
                                         "all": m["all"].as_bool().unwrap_or(false) && w.knows("find-all")});
                        let qq = q.clone();
                        w.command(format!("/find {}", worker::ascii_json(&req)), Box::new(move |mut r: Value| {
                            r["q"] = json!(qq);
                            done.lock().unwrap().push((to_browse, r));
                        }));
                    }
                }
                if waits {
                    if to_browse { self.browse.call("findWaiting", Value::Null) } else { self.panel.call("findWaiting", Value::Null) }
                }
            }
            // 비슷한 문서 찾기(비슷한 문서 찾기 설계서 §6) — 결과는 찾기와 같은 길(find_done → results)로, done.kind 로 나뉜다
            "similar" => {
                let path = m["path"].as_str().unwrap_or("").trim().to_string();
                let q = m["q"].as_str().unwrap_or("").to_string();
                let folder = m["folder"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
                if path.is_empty() {
                    return true;
                }
                crate::info!("browse", "비슷한 문서: {} (범위 {:?})", path, folder);
                let done = self.find_done.clone();
                match &self.worker {
                    None => {
                        let e = self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into());
                        done.lock().unwrap().push((to_browse, json!({"ok": false, "q": q, "why": e})));
                    }
                    Some(w) => {
                        // 이미 떠 있는 옛 워커는 /similar 를 모른다 — 새로 빌드하라고 알린다
                        if w.availability() == "up" && !w.knows("similar") {
                            done.lock().unwrap().push((to_browse, json!({"ok": false, "q": q,
                                "why": "워커가 비슷한 문서 찾기(/similar)를 모릅니다 — simplerag-rs.exe 를 새로 빌드하세요"})));
                            return true;
                        }
                        w.ensure_started();
                        w.note_use();
                        let waits = w.is_indexing();
                        let qq = q.clone();
                        w.command(format!("/similar {}", worker::ascii_json(&json!({"path": path, "folder": folder}))),
                            Box::new(move |mut r: Value| {
                                r["q"] = json!(qq);
                                done.lock().unwrap().push((to_browse, r));
                            }));
                        if waits {
                            if to_browse { self.browse.call("findWaiting", Value::Null) } else { self.panel.call("findWaiting", Value::Null) }
                        }
                    }
                }
            }
            "similar_pick" => {
                let dir = m["dir"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
                self.similar_pick(to_browse, dir);
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
            "properties" => {
                let path = m["path"].as_str().unwrap_or("");
                if let Err(e) = win::show_properties(path) {
                    crate::warn!("browse", "문서 속성을 열지 못했다: {} ({})", path, e);
                    let msg = format!("문서 속성을 열지 못했습니다: {}", e);
                    if to_browse { self.browse.call("notice", json!(msg)) } else { self.panel.notice(&msg) }
                }
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
            // AI 답변창 — 화면 목록의 문서를 문서별로 정리한다(다시 검색하지 않는다)
            "brief" => {
                let q = match one_line(m["q"].as_str().unwrap_or("")) {
                    Some(q) => q,
                    None => return true,
                };
                crate::info!("browse", "AI 정리 요청: {:?} (후보 {})", q.chars().take(40).collect::<String>(),
                             m["items"].as_array().map(|a| a.len()).unwrap_or(0));
                self.ask_to_browse = to_browse;
                let failed = match &self.worker {
                    // 이미 떠 있는 옛 워커는 /brief 를 모른다 — 새로 빌드하라고 알린다
                    Some(w) if w.availability() == "up" && !w.knows("brief") => {
                        Some("워커가 AI 정리(/brief)를 모릅니다 — simplerag-rs.exe 를 새로 빌드하세요".to_string())
                    }
                    Some(w) => {
                        // mode "summary" — 비슷한 문서 찾기 결과에서 기준 문서 요약(설계서)
                        w.brief(&json!({"q": q, "items": m["items"], "mode": m["mode"]}));
                        None
                    }
                    None => Some(self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into())),
                };
                if let Some(e) = failed {
                    if to_browse { self.browse.call("error", json!(e)) } else { self.panel.show_error(&e) }
                }
            }
            "brief_cancel" => {
                if let Some(w) = &self.worker {
                    w.cancel_brief();
                }
            }
            // 모두 찾기 — 문서들의 나머지 쪽 줄 · 쪽 본문. 결과는 찾기 결과와 같은 길로 돌아와 kind 로 나뉜다
            "find_doc" | "find_page" => {
                let kind = if m["type"] == "find_doc" { "doc" } else { "page" };
                let line = if kind == "doc" {
                    format!("/find-doc {}", worker::ascii_json(&json!({"kws": m["kws"], "paths": m["paths"]})))
                } else {
                    format!("/find-page {}", worker::ascii_json(&json!({"ids": m["ids"]})))
                };
                if let Some(w) = &self.worker {
                    let done = self.find_done.clone();
                    let req = m.clone();
                    w.command(line, Box::new(move |mut r: Value| {
                        r["kind"] = json!(kind);
                        r["req"] = req;
                        done.lock().unwrap().push((to_browse, r));
                    }));
                }
            }
            // 탐색기 화면 상태(창 켜짐 · 묶기 · 정렬 · 창 나눔 · 최근 검색) — 다음 실행에 되살린다
            "ui_state" => {
                let v = settings::ui_state_text(&m["state"]);
                if v != self.s.browse_state {
                    if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "BrowseState", &v) {
                        crate::warn!("browse", "화면 상태를 저장하지 못했다: {}", e);
                    }
                    self.s.browse_state = v;
                }
            }
            // 모두 찾기 체크 — 이 PC 에 기억한다
            "find_all" => {
                let on = m["on"].as_bool().unwrap_or(true);
                self.s.find_all = on;
                if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "FindAll", if on { "1" } else { "0" }) {
                    crate::warn!("browse", "모두 찾기 설정을 저장하지 못했다: {}", e);
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
            // 📌 항상 위 — 누른 창만 바꾼다(패널 · 탐색기 창 따로 기억)
            "ontop" => {
                let on = m["on"].as_bool().unwrap_or(false);
                self.set_on_top(to_browse, on);
            }
            // [나란히 보기] 단추 — 패널 · 탐색기 창 어디서 눌러도 같다
            "dock" => {
                let on = m["on"].as_bool().unwrap_or(true);
                // 탐색기 창에서 누르면 그 창이 보던 폴더("" = 검색 폴더 전체 → 맨 처음 검색 폴더)를 탐색기로 연다
                let folder = m["folder"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
                self.set_dock(on, if to_browse { "탐색기 창" } else { "패널" }, folder);
            }
            // [채팅 모드] · [탐색기 모드] 버튼 — 화면 모드를 바꿔 저장하고 그 모드의 화면으로 옮겨 간다
            "switch_mode" => {
                let mode = if m["mode"] == "rag" { "rag" } else { "explorer" };
                let folder = m["folder"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
                self.switch_ui_mode(mode, folder);
            }
            // 탐색 창 폴더 오른쪽 클릭 — 폴더 열기(그 폴더 안을 보여 준다) · 지금 인덱싱(그 폴더만)
            "open_folder" => {
                let path = m["path"].as_str().unwrap_or("");
                if let Err(e) = win::open_explorer(path) {
                    crate::warn!("browse", "폴더 열기 실패: {} ({})", path, e);
                    self.browse.call("error", json!(format!("폴더를 열지 못했습니다: {}", e)));
                }
            }
            "index_now" => {
                let path = m["path"].as_str().unwrap_or("");
                if let Some(j) = self.jobs.as_mut() {
                    j.approve_folder(path, "지금 인덱싱");
                }
            }
            // 탐색 창 하위 폴더 오른쪽 클릭 — 폴더 빼기 · 폴더 다시 넣기
            "exclude_folder" => {
                let path = m["path"].as_str().unwrap_or("").to_string();
                let msg = self.set_exclude(&path, m["on"].as_bool().unwrap_or(true)).unwrap_or_else(|e| e);
                self.browse.call("notice", json!(msg));
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
        // 사용자가 화면을 쓰는 중 — 쉬는 동안에만 하는 후보 만들기를 미룬다(pv_rect · ui_state 같은 자동 알림은 빼고)
        if !matches!(m["type"].as_str(), Some("pv_rect") | Some("ui_state") | Some("ready")) {
            self.t_user = Instant::now();
        }
        if m["type"] == "ready" {
            self.browse.on_ready();
            self.browse.call("setManual", json!(Self::manual_path().is_some()));
            let dock = self.s.panel_enabled;
            self.browse.call("setDock", json!(dock));
            let top = self.s.browse_on_top;
            self.browse.call("setOnTop", json!(top));
            // 이미 검색 폴더가 있는 사람(업데이트)에게는 안내를 자동으로 띄우지 않는다 — [?] 로만
            if !self.current_folders().is_empty() {
                self.mark_tour_done();
            }
            self.push_roots();
            self.ask_first_folder();
            return;
        }
        // 사용 안내를 끝까지 봤거나 건너뛰었다
        if m["type"] == "tour_done" {
            self.mark_tour_done();
            return;
        }
        // [?] → 사용 설명서 열기 — 기본 브라우저로
        if m["type"] == "open_manual" {
            let msg = match Self::manual_path() {
                Some(p) => match win::open_path(&p) {
                    Ok(()) => "사용 설명서를 열었습니다".to_string(),
                    Err(e) => format!("사용 설명서를 열지 못했습니다: {}", e),
                },
                None => "사용 설명서가 설치 폴더에 없습니다".to_string(),
            };
            self.browse.call("notice", json!(msg));
            return;
        }
        // 명령줄 오른쪽 [⚙ 설정] — 트레이 메뉴의 설정과 같은 창. 고를 후보가 있으면 그 탭으로
        if m["type"] == "settings" {
            if m["cand"].as_bool().unwrap_or(false) {
                self.open_candidates();
            } else {
                self.show_info();
            }
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
    // [+] — 파일 고르기 → upload_paths (문서 업로드 설계서 §4·§10)
    //   conv = 패널이 정한 대화 id · have = 이미 올린 문서 수 · names = 칩에 이미 있는 이름(같은 이름 막기)
    //------------------------------------------------------------------
    fn upload_pick(&mut self, conv: &str, have: usize, names: &[String]) {
        if !self.upload_ready() {
            return;
        }
        // 대화상자 제목에 한도와 남은 자리를 보인다 — 고르기 전에 알 수 있게
        let left = UPLOAD_MAX_FILES.saturating_sub(have);
        let title = format!("질문할 문서 고르기 — 최대 {}개 · 파일당 {}MB 이하 (남은 자리 {}개)", UPLOAD_MAX_FILES, UPLOAD_MAX_MB, left);
        let picked = rfd::FileDialog::new().set_title(&title)
            .add_filter("문서", UPLOAD_EXTS).pick_files().unwrap_or_default();
        self.upload_paths(conv, have, names, picked);
    }

    //------------------------------------------------------------------
    // 업로드할 수 있는 상태인가 — 워커가 없거나 업로드를 모르면 패널에 알리고 false
    //------------------------------------------------------------------
    fn upload_ready(&mut self) -> bool {
        let w = match &self.worker {
            Some(w) => w.clone(),
            None => {
                self.panel.call("uploadQueued", json!([]));
                self.panel.show_error(&self.worker_error.clone().unwrap_or("SimpleRAG 를 찾지 못했습니다".into()));
                return false;
            }
        };
        if w.availability() == "up" && !w.knows("upload") {
            self.panel.call("uploadQueued", json!([]));
            self.panel.set_state("이 SimpleRAG 는 문서 업로드를 모릅니다 — 새로 빌드한 simplerag 가 필요합니다");
            return false;
        }
        true
    }

    //------------------------------------------------------------------
    // 패널 끌어 놓기 (채팅 첨부 설계서 §2-3)
    //=> 들어옴/나감은 입력 상자 강조만, 놓음은 경로를 패널에 넘겨 대화 id · 올린 수와 함께 upload_paths 로 되돌려 받는다
    //   (대화 id 와 칩 상태는 패널 JS 가 갖고 있다). 탐색기 모드 화면이면 비슷한 문서 찾기 입력란으로 받는다
    //------------------------------------------------------------------
    fn on_panel_drop(&mut self, e: wry::DragDropEvent) {
        if self.panel_mode == "explorer" {
            self.similar_drop(false, e);
            return;
        }
        match e {
            wry::DragDropEvent::Enter { paths, .. } => self.panel.call("dropHint", json!({"on": true, "n": paths.len()})),
            wry::DragDropEvent::Leave => self.panel.call("dropHint", json!({"on": false})),
            wry::DragDropEvent::Drop { paths, .. } => {
                self.panel.call("dropHint", json!({"on": false}));
                let list: Vec<String> = paths.iter().map(|p| p.to_string_lossy().into_owned()).collect();
                crate::info!("panel", "끌어 놓은 파일 {}개", list.len());
                self.panel.call("dropFiles", json!(list));
            }
            _ => {}
        }
    }

    //------------------------------------------------------------------
    // 비슷한 문서 찾기 — 끌어 놓기 (비슷한 문서 찾기 설계서 §4-2)
    //=> 창 어디에 놓아도 이벤트는 오지만 입력란 위에 놓을 때만 받는다. 입력란인지는 화면 JS 가
    //   좌표로 판단하므로 좌표를 그대로 넘긴다(웹뷰 왼쪽 위 기준).
    //    1) 들어옴 · 움직임 → dropOver{x, y, n} (입력란 강조) / 나감 → dropOver(null)
    //    2) 놓음 → 첫 파일을 검사해 dropAt{x, y, path | why, many}
    //
    // -in: to_browse = MoAI 탐색기 창이면 true, 탐색기 옆 패널(탐색기 모드)이면 false / e = 끌어 놓기 이벤트
    //
    // -out: 없음
    // -out: error = 없음 (거절 이유는 화면에 넘긴다)
    //------------------------------------------------------------------
    fn similar_drop(&mut self, to_browse: bool, e: wry::DragDropEvent) {
        let (func, arg) = match e {
            wry::DragDropEvent::Enter { paths, position } => ("dropOver", json!({"x": position.0, "y": position.1, "n": paths.len()})),
            wry::DragDropEvent::Over { position } => ("dropOver", json!({"x": position.0, "y": position.1})),
            wry::DragDropEvent::Leave => ("dropOver", Value::Null),
            wry::DragDropEvent::Drop { paths, position } => {
                let mut a = json!({"x": position.0, "y": position.1, "many": paths.len() > 1});
                match paths.first() {
                    Some(p) => match similar_reject(p) {
                        None => a["path"] = json!(p.to_string_lossy()),
                        Some(why) => a["why"] = json!(why),
                    },
                    None => a["why"] = json!("놓은 파일이 없습니다"),
                }
                crate::info!("browse", "비슷한 문서 — 끌어 놓은 파일 {}개", paths.len());
                ("dropAt", a)
            }
            _ => return,
        };
        if to_browse { self.browse.call(func, arg) } else { self.panel.call(func, arg) }
    }

    //------------------------------------------------------------------
    // 비슷한 문서 찾기 — [+] 파일 고르기 (설계서 §4-1)
    //=> 지원 형식만 보이는 파일 창으로 문서 하나를 고른다. [항상 위] 켬에서도 창 뒤로 숨지 않게 부모 창을 정하고,
    //   보던 폴더에서 연다. 이름 칸에 *.* 를 쳐서 다른 파일을 고를 수 있으므로 고른 뒤 다시 검사한다.
    //
    // -in: to_browse = MoAI 탐색기 창이면 true / dir = 처음 열 폴더(없으면 None)
    //
    // -out: 없음 (고르면 화면 similarPicked, 거절이면 notice)
    // -out: error = 없음
    //------------------------------------------------------------------
    fn similar_pick(&mut self, to_browse: bool, dir: Option<String>) {
        let mut d = rfd::FileDialog::new().set_title("비슷한 문서를 찾을 기준 문서 고르기").add_filter("문서", UPLOAD_EXTS);
        if let Some(dir) = dir.filter(|p| std::path::Path::new(p).is_dir()) {
            d = d.set_directory(dir);
        }
        d = if to_browse { d.set_parent(&self.browse.window) } else { d.set_parent(&self.panel.window) };
        let p = match d.pick_file() {
            Some(p) => p,
            None => return,
        };
        let (func, arg) = match similar_reject(&p) {
            None => ("similarPicked", json!({"path": p.to_string_lossy()})),
            Some(why) => ("notice", json!(format!("비슷한 문서를 찾을 수 없습니다: {}", why))),
        };
        if to_browse { self.browse.call(func, arg) } else { self.panel.call(func, arg) }
    }

    //------------------------------------------------------------------
    // 파일 목록 → 한도 확인 · 워커에 /upload 맡기기 ([+] 와 끌어 놓기 공통)
    //   conv = 대화 id · have = 이미 올린 문서 수 · names = 이미 있는 칩 이름 · picked = 올릴 경로들
    //------------------------------------------------------------------
    fn upload_paths(&mut self, conv: &str, have: usize, names: &[String], picked: Vec<std::path::PathBuf>) {
        if picked.is_empty() {
            self.panel.call("uploadQueued", json!([]));
            return;
        }
        if !self.upload_ready() {
            return;
        }
        let w = match &self.worker {
            Some(w) => w.clone(),
            None => return,
        };
        let mut list = vec![];
        let mut n = have;
        // 같은 이름 막기 — 칩 · 빼기[×] · 결과 맞추기가 모두 이름 기준이다
        let mut seen: std::collections::HashSet<String> = names.iter().map(|s| s.to_lowercase()).collect();
        for p in picked {
            let path = p.to_string_lossy().into_owned();
            let name = p.file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| path.clone());
            let ext = p.extension().map(|s| s.to_string_lossy().to_lowercase()).unwrap_or_default();
            let meta = std::fs::metadata(&p).ok();
            let size = meta.as_ref().map(|m| m.len()).unwrap_or(0);
            let why = upload_reject(meta.as_ref().map(|m| m.is_dir()).unwrap_or(false), &ext, size,
                                    n, seen.contains(&name.to_lowercase()));
            if let Some(why) = why {
                list.push(json!({"name": name, "ok": false, "why": why}));
                continue;
            }
            seen.insert(name.to_lowercase());
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
            UserEvent::PanelDrop(e) => self.on_panel_drop(e),
            UserEvent::BrowseDrop(e) => self.similar_drop(true, e),
            // 로그 판 — 스레드가 다 모았다
            UserEvent::LogView(v) => {
                let _ = self.info.webview.evaluate_script(&format!("rsb.logView({})", v));
            }
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
            // 답 · AI 정리는 요청한 화면으로(탐색기 창의 AI 답변창이면 그 창)
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
        let msg = plain_error(msg);
        if self.ask_to_browse && self.browse.visible {
            self.browse.call("error", json!(msg));
        } else if self.panel.visible {
            self.panel.show_error(&msg);
        } else {
            self.tray.notify("MoAI-S", &msg);
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
            // [나란히 보기] 끔 — 패널을 붙이지는 않고, 탐색기에서 연 검색 폴더를 질문 폴더로 따라간다.
            //   검색 폴더가 아닌 곳으로 가면(None) 이전 폴더를 그대로 둔다
            if let Some(f) = folder {
                if self.panel.folder.as_deref() != Some(f.as_str()) {
                    crate::info!("panel", "일반 창 질문 폴더 바뀜: {}", f);
                    self.panel.folder = Some(f.clone());
                    self.panel.call("setFolder", json!(f));
                    self.refresh_scope();
                }
                self.follow_root(&f);
                // 그 탐색기 창 — [탐색기에서 보기] 가 근거 파일을 거기서 찾는다
                self.answer_hwnd = hwnd;
            }
            return;
        }
        match folder {
            Some(f) => {
                self.panel.show_for(hwnd, &f, &self.s);
                self.follow_root(&f);
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
                    if win::is_window(hwnd) {
                        // 검색 폴더가 아닌 곳으로 옮겨 갔다 — 패널은 그대로 붙여 두고 범위도 이전 검색 폴더를 지킨다
                        crate::info!("panel", "탐색기가 검색 폴더 밖으로 옮겨 갔다 — 패널은 그대로 둔다");
                    } else {
                        // 탐색기 창을 닫았다 — 패널은 닫지 않고 그 자리에 따로 남긴다
                        self.panel.detach();
                        self.monitor.watch_window(None);
                    }
                }
            }
        }
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
    // 워커 인덱스 상태 파일(index_state.json) — 데이터 폴더 바로 아래(simplerag config.rs 의 state_path 와 같다)
    //------------------------------------------------------------------
    fn state_path(&self) -> Option<String> {
        Some(std::path::Path::new(&self.data_home()?).join("index_state.json").to_string_lossy().into_owned())
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
        // 채팅 질문 범위 = [+] 옆에서 고른 검색 폴더. 안 골랐으면 모든 검색 폴더(문서 수는 검색 폴더마다 센 합)
        let folder = match self.panel.sel.clone() {
            Some(f) => f,
            None => {
                let n: Option<usize> = self.panel_folders().map(|(_, total)| total);
                return format!("범위: 모든 검색 폴더 · 문서 {}건", n.map(|x| x.to_string()).unwrap_or("?".into()));
            }
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

    //------------------------------------------------------------------
    // 채팅 [+] 옆 검색 폴더 목록 — (검색 폴더마다 {path, name, n}, 모든 검색 폴더 문서 수)
    //=> 상태 파일의 문서 키를 경로 앞부분으로 센다. 상태 파일을 못 읽으면 None
    //------------------------------------------------------------------
    fn panel_folders(&mut self) -> Option<(Vec<Value>, usize)> {
        let keys = self.state_keys()?;
        let mut items = vec![];
        let mut total = 0;
        for f in self.current_folders() {
            let base = format!("{}\\", scope::norm(&f).trim_end_matches('\\'));
            let n = keys.iter().filter(|k| k.starts_with(&base)).count();
            total += n;
            let name = f.trim_end_matches(['\\', '/']).rsplit(['\\', '/']).next().filter(|s| !s.is_empty()).unwrap_or(&f).to_string();
            items.push(json!({"path": f, "name": name, "n": n}));
        }
        Some((items, total))
    }

    //------------------------------------------------------------------
    // 검색 폴더 목록 · 고른 값 · 범위 글을 채팅 화면에 보낸다
    //   고른 폴더가 목록에서 빠졌으면(폴더 빼기) 모든 검색 폴더로 되돌린다
    //------------------------------------------------------------------
    fn push_panel_folders(&mut self) {
        let roots = self.current_folders();
        if let Some(sel) = self.panel.sel.clone() {
            if !roots.iter().any(|r| r.eq_ignore_ascii_case(&sel)) {
                self.panel.sel = None;
            }
        }
        let (items, total) = match self.panel_folders() {
            Some(x) => x,
            None => (roots.iter().map(|f| json!({"path": f, "name": f, "n": Value::Null})).collect(), 0),
        };
        let sel = self.panel.sel.clone();
        self.panel.call("setFolders", json!({"items": items, "all_n": total, "sel": sel}));
        self.refresh_scope();
    }

    //------------------------------------------------------------------
    // 탐색기에서 연 폴더를 따라 질문 범위를 그 검색 폴더로 — 다른 검색 폴더로 옮겼을 때만 바꾼다
    //   (같은 검색 폴더 안에서 하위 폴더를 오가면 사용자가 [+] 옆에서 고른 값을 지킨다)
    //------------------------------------------------------------------
    fn follow_root(&mut self, folder: &str) {
        let (ok, root) = self.scope.lock().unwrap().contains(folder);
        if !ok || self.panel.auto_root.as_deref() == Some(root.as_str()) {
            return;
        }
        crate::info!("panel", "질문 범위를 탐색기 폴더의 검색 폴더로: {}", root);
        self.panel.auto_root = Some(root.clone());
        self.panel.sel = Some(root);
        self.push_panel_folders();
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
        if m["type"] != "ready" {
            self.t_user = Instant::now();
        }
        match m["type"].as_str().unwrap_or("") {
            "ready" => {
                self.panel.on_ready();
                let h = self.history_list();
                self.panel.call("setHistory", h);
                if let Some(f) = self.panel.folder.clone() {
                    self.panel.call("setFolder", json!(f));
                }
                // [+] 옆 검색 폴더 목록과 고른 값(범위 글도 함께)
                self.push_panel_folders();
                if self.panel_mode == "explorer" {
                    self.push_roots();
                }
                // [나란히 보기] 단추 상태
                let dock = self.s.panel_enabled;
                self.panel.call("setDock", json!(dock));
                let top = self.s.panel_on_top;
                self.panel.call("setOnTop", json!(top));
            }
            "ask" => {
                // 채팅 질문이 먼저다 — 쓰던 AI 정리는 멈추고 그 화면에 알린다
                if let Some(w) = &self.worker {
                    if w.cancel_brief() {
                        let code = "rsb.briefStopped()".to_string();
                        if self.ask_to_browse { self.browse.js(code) } else { self.panel.js(code) }
                    }
                }
                self.ask_to_browse = false;
                let q = match one_line(m["text"].as_str().unwrap_or("")) {
                    Some(q) => q,
                    None => return,
                };
                // 질문 범위 = [+] 옆에서 고른 검색 폴더(None 이면 모든 검색 폴더)
                let folder = self.panel.sel.clone();
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
                self.upload_pick(&conv, m["have"].as_u64().unwrap_or(0) as usize, &str_list(&m["names"]));
            }
            "upload_paths" => {
                // 끌어 놓은 파일 — 패널이 대화 id · 올린 수를 붙여 되돌려 보냈다
                let conv = m["conv"].as_str().unwrap_or("").to_string();
                let paths = str_list(&m["paths"]).into_iter().map(std::path::PathBuf::from).collect();
                self.upload_paths(&conv, m["have"].as_u64().unwrap_or(0) as usize, &str_list(&m["names"]), paths);
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
                // 일반 창(붙은 탐색기 없음)이면 질문 범위 폴더를 탐색기로 열어 준다 — 그 창 검색으로 이름을 찾을 수 있다
                if !self.panel.docked() && !docs.is_empty() && !(self.answer_hwnd != 0 && win::is_window(self.answer_hwnd)) {
                    if let Some(f) = self.panel.folder.clone() {
                        let _ = win::open_explorer(&f);
                        self.panel.notice("탐색기에서 질문 폴더를 열었습니다 — 근거 파일 이름으로 찾아보세요");
                    }
                    return;
                }
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
            // [+] 옆 검색 폴더 고르기 — path 가 비면 모든 검색 폴더
            "pick_folder" => {
                let p = m["path"].as_str().unwrap_or("").to_string();
                crate::info!("panel", "질문 범위 고름: {}", if p.is_empty() { "모든 검색 폴더" } else { &p });
                self.panel.sel = if p.is_empty() { None } else { Some(p) };
                self.push_panel_folders();
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
            // 사전 후보 알림을 눌렀다
            "candidates" => self.open_candidates(),
            // 트레이 "사용 안내" — 탐색기 창을 열고 1단계부터
            "tour" => {
                self.open_browse(None, None, Value::Null);
                self.browse.call("startTour", Value::Null);
            }
            // 화면 열기 — 탐색기 모드면 MoAI 탐색기 창, RAG 모드면 지금처럼 탐색기 옆 패널
            // 탐색기 모드라도 [나란히 보기] 켬이면 Windows 탐색기 옆 탐색기 모드 패널로 띄운다
            "open" if self.s.ui_mode == "explorer" && !(self.s.panel_enabled && !self.current_folders().is_empty()) => self.open_browse(None, None, Value::Null),
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
            "add_folder" => {
                let msg = self.add_folder_dialog();
                if !msg.is_empty() {
                    self.tray.notify("MoAI-S", &msg);
                }
            }
            _ => {}
        }
    }

    // 화면 열기 — 범위 탐색기가 있으면 그 옆에, 없으면 첫 범위 폴더를 탐색기로 연다
    fn open_panel(&mut self) {
        // 나란히 보기를 껐으면 탐색기를 열지 않고 채팅 창을 일반 창으로
        if !self.s.panel_enabled {
            return self.open_panel_window(None);
        }
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

    //------------------------------------------------------------------
    // 그 폴더를 열고 옆에 패널 띄우기 — 폴더를 이미 보는 탐색기 창이 있으면 그 창을 앞으로, 없으면 새로 연다
    //   (새 창은 폴더 감시가 알아채 패널을 띄운다). folder 가 없으면 맨 처음 인덱싱 폴더
    //------------------------------------------------------------------
    fn open_panel_at(&mut self, folder: Option<String>) {
        if !self.s.panel_enabled {
            return self.open_panel_window(folder);
        }
        let first = self.scope.lock().unwrap().active.first().cloned();
        let target = match folder.or(first) {
            Some(t) => t,
            None => return self.tray.notify("MoAI-S", SCOPE_WARN),
        };
        let norm = |p: &str| p.trim_end_matches(['\\', '/']).to_lowercase();
        let open = self.monitor.in_scope_windows().into_iter().find(|(_, f)| norm(f) == norm(&target));
        if let Some((h, f)) = open {
            win::bring_to_front(h);
            self.panel.forget_closed(h);
            self.on_folder(h, Some(f));
            self.panel.activate();
            return;
        }
        crate::info!("app", "채팅 모드: 폴더를 탐색기로 연다 — {}", target);
        if win::open_explorer(&target).is_err() {
            self.tray.notify("MoAI-S", &format!("탐색기를 열지 못했습니다: {}", target));
        }
    }

    //------------------------------------------------------------------
    // 채팅 화면을 일반 창으로 열기 ([나란히 보기] 끔)
    //=> 탐색기를 열지 않고 패널 창을 화면 가운데 보통 창으로 띄운다. 질문 범위는 고른 폴더(없으면 지금 폴더 → 맨 처음 인덱싱 폴더).
    //   folder = 탐색 창에서 고른 폴더
    //------------------------------------------------------------------
    fn open_panel_window(&mut self, folder: Option<String>) {
        let first = self.scope.lock().unwrap().active.first().cloned();
        let target = match folder.or_else(|| self.panel.folder.clone()).or(first) {
            Some(t) => t,
            None => return self.tray.notify("MoAI-S", SCOPE_WARN),
        };
        // 탐색기를 따라가지 않는다 — 감시 대상 창도 비운다
        self.monitor.watch_window(None);
        let s = self.s.clone();
        self.panel.show_window(&target, &s);
        self.follow_root(&target);
        self.refresh_scope();
        if let Some(w) = &self.worker {
            if self.s.start_mode != "lazy" {
                w.ensure_started();
            }
        }
    }

    //------------------------------------------------------------------
    // [나란히 보기] 켜기 · 끄기 — 설정 창 · 패널 · 탐색기 창의 단추 공통 (INI [Panel] Enabled)
    //=> 값을 저장하고 모든 화면의 단추를 맞춘 뒤, 지금 떠 있는 창을 새 방식으로 옮긴다.
    //    · 끔: 탐색기 옆 채팅 패널 → 일반 창으로 / 탐색기 옆 탐색기 모드 패널 → 숨기고 MoAI 탐색기 창
    //    · 켬: 일반 창으로 뜬 채팅 → 그 폴더를 탐색기로 열어 옆에 붙인다
    //          MoAI 탐색기 창 → 창을 숨기고 보던 폴더를 Windows 탐색기로 열어 오른쪽에 탐색기 모드 패널을 붙인다
    //   on = 나란히 보기 켬 / from = 누른 곳(로그용) / folder = 탐색기 창이 보던 폴더(None = 맨 처음 검색 폴더)
    //------------------------------------------------------------------
    fn set_dock(&mut self, on: bool, from: &str, folder: Option<String>) {
        crate::info!("app", "나란히 보기: {} ({})", if on { "켬" } else { "끔" }, from);
        self.s.panel_enabled = on;
        if let Err(e) = settings::save_ini_value(&self.s.ini_path, "Panel", "Enabled", if on { "1" } else { "0" }) {
            crate::warn!("app", "나란히 보기 설정을 저장하지 못했다: {}", e);
        }
        self.panel.call("setDock", json!(on));
        self.browse.call("setDock", json!(on));
        let note = if on { "나란히 보기를 켰습니다 — 탐색기에서 검색 폴더를 열면 오른쪽에 나란히 뜹니다" }
                   else { "나란히 보기를 껐습니다 — 따로 창으로 띄웁니다" };
        if on {
            // 일반 창으로 떠 있던 채팅을 탐색기 옆으로
            if self.panel.visible && !self.panel.docked() {
                let f = self.panel.folder.clone();
                self.panel.hide(false);
                self.open_panel_at(f);
            } else if self.browse.visible && self.panel_mode == "explorer" {
                // MoAI 탐색기 창 → 보던 폴더를 Windows 탐색기로 열고(이미 열려 있으면 앞으로) 옆에 패널을 붙인다.
                //   새로 연 탐색기 창은 폴더 감시가 알아채 패널을 띄운다(채팅 모드 나란히 보기와 같은 길)
                self.browse.hide();
                self.open_panel_at(folder);
            } else {
                self.browse.call("notice", json!(note));
            }
        } else if self.panel.docked() {
            let f = self.panel.folder.clone();
            if self.panel_mode == "rag" {
                self.open_panel_window(f);
            } else {
                // 탐색기 모드 패널은 MoAI 탐색기 창이 일반 창 몫이다
                self.panel.hide(true);
                self.monitor.watch_window(None);
                self.open_browse(None, f, Value::Null);
            }
        } else {
            self.browse.call("notice", json!(note));
        }
        self.panel.call("notice", json!(note));
    }

    //------------------------------------------------------------------
    // 📌 항상 위에 표시 켜기 · 끄기 — 다른 창에 가리지 않게
    //=> 누른 창(패널 또는 MoAI 탐색기 창)만 바꾸고 INI 에 남긴다. 다음 실행 때 창을 만들며 되살린다.
    //   browse = MoAI 탐색기 창이면 true, 패널(채팅 · 탐색기 옆)이면 false / on = 항상 위
    //------------------------------------------------------------------
    fn set_on_top(&mut self, browse: bool, on: bool) {
        let key = if browse { "BrowseOnTop" } else { "PanelOnTop" };
        crate::info!("app", "항상 위에 표시: {} {}", if browse { "탐색기 창" } else { "패널" }, if on { "켬" } else { "끔" });
        if browse {
            self.s.browse_on_top = on;
            win::set_topmost(self.browse.hwnd(), on);
            self.browse.call("setOnTop", json!(on));
        } else {
            self.s.panel_on_top = on;
            win::set_topmost(self.panel.hwnd(), on);
            self.panel.call("setOnTop", json!(on));
        }
        if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", key, if on { "1" } else { "0" }) {
            crate::warn!("app", "항상 위 설정을 저장하지 못했다: {}", e);
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

    //------------------------------------------------------------------
    // '지금 인덱싱' 을 [예] · [아니요] 창으로 묻기 — 트레이 풍선에는 단추를 넣을 수 없다
    //=> 창은 답할 때까지 멈추므로 따로 스레드에서 띄운다. 이미 떠 있으면 하나만 둔다
    //   ([예] 는 기다리는 폴더 전부를 넣으니 창이 둘일 까닭이 없다).
    //   [아니요] 면 그대로 기다린다 — 문서 수가 바뀌면 다시 묻는다.
    // -in: msg = 무엇이 기다리는지
    //------------------------------------------------------------------
    fn ask_index_now(&self, msg: &str) {
        static OPEN: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
        if OPEN.swap(true, std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        let p = self.proxy.clone();
        let text = format!("{}\n\n지금 인덱싱할까요?", msg);
        std::thread::spawn(move || {
            let yes = win::ask_yes_no("MoAI-S", &text);
            OPEN.store(false, std::sync::atomic::Ordering::SeqCst);
            crate::info!("app", "지금 인덱싱 물음: {}", if yes { "예" } else { "아니요" });
            if yes {
                post(&p, UserEvent::Tray("index_now".into()));
            }
        });
    }

    fn on_autoindex(&mut self, kind: &str, msg: &str) {
        if kind == "ask" {
            crate::info!("app", "자동 인덱싱 물음: {}", msg);
            self.ask_index_now(msg);
            self.update_tooltip();
            return;
        }
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
            let (l, t, r, b) = win::work_area_at(cx, cy);
            // 먼저 그 모니터로 옮긴다 — 배율이 다른 모니터(200% ↔ 100%)면 이때 Windows 가 창 배율을 바꾼다.
            //   예전에는 옮기기 전 배율로 잰 크기로 자리를 정해, 창이 화면 위로 넘치거나 꽉 찼다
            self.info.window.set_outer_position(tao::dpi::PhysicalPosition::new(l, t));
            // 크기는 그 모니터 작업 영역에 맞춘다 — 720×720(논리)을 넘지 않고 높이는 85% 까지(넘치면 안에서 스크롤)
            let k = self.info.window.scale_factor();
            let (ww, wh) = ((r - l) as f64 / k, (b - t) as f64 / k);
            self.info.window.set_inner_size(tao::dpi::LogicalSize::new(INFO_W.min(ww - 32.0), INFO_H.min(wh * 0.85)));
            let o = self.info.window.outer_size();
            self.info.window.set_outer_position(tao::dpi::PhysicalPosition::new((r - o.width as i32 - 16).max(l), (b - o.height as i32 - 16).max(t)));
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
        // 메뉴 "로그 (N)" — 상태 파일이 바뀔 때만 다시 센다(logview 캐시)
        if let Some(p) = self.state_path() {
            info["n_failed"] = json!(logview::count_failed(&p));
        }
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
        // 폴더마다 문서 수 · 진행 · 대기 — 탐색 창 폴더 목록과 같은 값
        info["roots"] = self.roots_value();
        info["action_msg"] = json!(self.info.action_msg);
        info["folder_msg"] = json!([self.info.folder_msg.as_str(), self.icon_msg.as_str()].iter()
            .filter(|s| !s.is_empty()).cloned().collect::<Vec<_>>().join("\n"));
        info["exts"] = json!(self.s.index_exts);
        info["exts_default"] = json!(settings::DEFAULT_EXTS);
        info["ext_msg"] = json!(self.info.ext_msg);
        info["ui_mode"] = json!(self.s.ui_mode);
        info["dock"] = json!(self.s.panel_enabled);
        info["mode_msg"] = json!(self.mode_msg);
        info["page_click"] = json!(self.s.page_click);
        info["folder_icon"] = json!(self.s.folder_icon);
        info["cand_notify"] = json!(self.s.cand_notify);
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
        if m["type"] != "ready" {
            self.t_user = Instant::now();
        }
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
            "set_exts" => {
                let raw = m["exts"].as_array().map(|a| a.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(" ")).unwrap_or_default();
                self.info.ext_msg = self.set_exts(&raw).unwrap_or_else(|e| e);
            }
            // 화면 모드(탐색기 · AI) — ini 에 남기고 패널 화면을 바로 바꾼다(다시 시작 없이)
            "ui_mode" => {
                let mode = if m["value"] == "rag" { "rag" } else { "explorer" };
                self.save_ui_mode(mode);
                self.set_panel_mode(mode);
            }
            "dock" => {
                let on = m["value"].as_bool().unwrap_or(true);
                self.set_dock(on, "설정 창", None);
            }
            "page_click" => {
                let v = if m["value"] == "app" { "app" } else { "preview" };
                self.s.page_click = v.to_string();
                if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "PageClick", v) {
                    self.info.action_msg = e;
                }
                self.push_roots();
            }
            // 사전 후보 알림 켜기 · 끄기 — INI [UI] CandidateNotify
            "cand_notify" => {
                let on = m["on"].as_bool().unwrap_or(true);
                self.s.cand_notify = on;
                if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "CandidateNotify", if on { "1" } else { "0" }) {
                    crate::warn!("app", "사전 후보 알림 설정을 저장하지 못했다: {}", e);
                }
            }
            "folder_icon" => {
                let on = m["on"].as_bool().unwrap_or(true);
                self.s.folder_icon = on;
                self.icon_msg = if on { "검색 폴더 아이콘을 바꾸는 중…".into() } else { "아이콘을 원래대로 돌리는 중…".into() };
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
            // 로그 판 — 열 때 · [새로고침]. 상태 파일 2MB · 로그 7일치를 읽으므로 스레드에서
            "log_view" => {
                let (sp, p) = (self.state_path(), self.proxy.clone());
                std::thread::spawn(move || {
                    let v = logview::build(sp.as_deref());
                    post(&p, UserEvent::LogView(v));
                });
            }
            "log_open" | "log_reveal" => {
                let path = m["path"].as_str().unwrap_or("");
                let r = if m["type"] == "log_open" { win::open_path(path) } else { win::reveal(path) };
                if let Err(e) = r {
                    crate::warn!("app", "로그 판에서 문서를 열지 못했다: {} ({})", path, e);
                    // 로그 판 실패 문서 묶음 위 안내 줄에 보인다(retryResult 의 실패 꼴과 같다)
                    let msg = format!("열지 못했습니다: {}", e);
                    let _ = self.info.webview.evaluate_script(&format!("rsb.retryResult({}, false, {})", json!(path), json!(msg)));
                }
            }
            "retry_doc" => {
                let path = m["path"].as_str().unwrap_or("");
                let msg = match self.jobs.as_mut() {
                    None => Err("자동 인덱싱할 폴더가 없습니다".to_string()),
                    Some(j) => j.retry_doc(path),
                };
                let (ok, msg) = match msg { Ok(t) => (true, t), Err(t) => (false, t) };
                let _ = self.info.webview.evaluate_script(&format!("rsb.retryResult({}, {}, {})", json!(path), json!(ok), json!(msg)));
                self.update_tooltip();
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
            "syn_suggest" => {
                // 직접 만들었으면 지금까지의 놓친 질문은 반영된 것 — 자동 만들기는 여기서부터 센다
                if let Some(home) = self.data_home() {
                    self.cand.syn_base = synonyms::counts(&home).0;
                    self.cand.syn_at = candidates::now();
                    self.save_cand_state();
                }
                self.info.syn_msg = self.suggest_synonyms();
            }
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
    // 인덱싱 확장자 바꾸기 — INI [AutoIndex] Extensions 저장, 감시 · 워커 · 자동 인덱싱에 알리기
    //=> 워커는 시작할 때만 확장자를 읽으므로(set_flag 와 같이) 올라가 있으면 다시 올린다.
    //   뺀 확장자의 문서는 다음 대조에서 인덱스에서 빠지고, 늘린 확장자의 문서는 들어간다.
    // -in: raw = 공백으로 이은 확장자들
    // -out: Ok(안내 글) / Err(사유 — 저장하지 않았다)
    //------------------------------------------------------------------
    fn set_exts(&mut self, raw: &str) -> Result<String, String> {
        let (exts, bad) = settings::parse_exts(raw);
        if !bad.is_empty() {
            return Err(format!("확장자로 쓸 수 없습니다: {}", bad.join(" ")));
        }
        if exts.is_empty() {
            return Err("확장자가 하나는 있어야 합니다".into());
        }
        if exts == self.s.index_exts {
            return Ok(String::new());
        }
        if self.index_running() {
            return Err("인덱싱이 돌고 있습니다 — 끝난 뒤 다시 적용하세요".into());
        }
        settings::save_ini_value(&self.s.ini_path, "AutoIndex", "Extensions", &exts.join(" ")).map_err(|e| {
            crate::warn!("app", "인덱싱 확장자를 저장하지 못했다: {}", e);
            e
        })?;
        crate::info!("app", "인덱싱 확장자를 바꿨다: {}", exts.join(" "));
        self.s.index_exts = exts.clone();
        watcher::set_exts(&exts);
        Ok(self.apply_index_filter("인덱싱 확장자 변경", true))
    }

    //------------------------------------------------------------------
    // 인덱싱 폴더 아래 하위 폴더 빼기 · 다시 넣기 — INI [AutoIndex] ExcludeFolders 저장, 워커 · 자동 인덱싱에 알리기
    //=> 뺀 폴더의 문서는 다음 대조에서 인덱스에서 빠진다(원본 파일은 그대로). 다시 넣으면 처음부터 인덱싱한다.
    //   기다리던 새 문서까지 허락한 것은 아니므로 AskAboveDocs 는 그대로 묻는다.
    // -in: path = 하위 폴더 / on = true 면 빼기, false 면 다시 넣기
    // -out: Ok(안내 글) / Err(사유 — 저장하지 않았다)
    //------------------------------------------------------------------
    fn set_exclude(&mut self, path: &str, on: bool) -> Result<String, String> {
        let norm = |p: &str| p.trim_end_matches(['\\', '/']).to_lowercase();
        let target = norm(path);
        if on && !self.current_folders().iter().any(|r| target.starts_with(&format!("{}\\", norm(r)))) {
            return Err("검색 폴더의 하위 폴더만 뺄 수 있습니다".into());
        }
        if self.index_running() {
            return Err("인덱싱이 돌고 있습니다 — 끝난 뒤 다시 하세요".into());
        }
        // 이 폴더와 그 안쪽 항목을 지운다 — 바깥 폴더를 빼면 안쪽 항목은 필요 없다
        let mut v: Vec<String> = self.s.exclude_folders.iter().filter(|e| {
            let low = norm(e);
            low != target && !low.starts_with(&format!("{}\\", target))
        }).cloned().collect();
        if on {
            v.push(path.trim_end_matches(['\\', '/']).to_string());
        }
        settings::save_ini_value(&self.s.ini_path, "AutoIndex", "ExcludeFolders", &v.join(";")).map_err(|e| {
            crate::warn!("app", "뺀 폴더를 저장하지 못했다: {}", e);
            e
        })?;
        crate::info!("app", "하위 폴더 {}: {}", if on { "빼기" } else { "다시 넣기" }, path);
        self.s.exclude_folders = v;
        self.push_roots();
        Ok(self.apply_index_filter("인덱싱 폴더 변경", false))
    }

    // 인덱싱이 돌고 있는가 — 인덱싱 대상을 바꾸면 안 되는 때
    fn index_running(&self) -> bool {
        self.jobs.as_ref().map(|j| {
            let st = j.short_state();
            st.starts_with("인덱싱 중") || st.starts_with("인덱스 확인 중")
        }).unwrap_or(false)
    }

    //------------------------------------------------------------------
    // 인덱싱 대상(확장자 · 뺀 폴더)이 바뀐 뒤 — 워커를 다시 올리고 폴더 대조를 부탁한다
    //=> 워커는 시작할 때만 대상을 읽으므로 올라가 있으면 다시 올린다(뜰 때 폴더를 대조한다).
    // -in: why = 로그에 남길 사유 / approve = 다음 대조를 허락받은 것으로 칠까(jobs.filter_changed)
    // -out: 안내 글
    //------------------------------------------------------------------
    fn apply_index_filter(&mut self, why: &str, approve: bool) -> String {
        let mut restart = false;
        if let Some(w) = &self.worker {
            w.set_env("SIMPLERAG_INDEX_EXTS", &self.s.index_exts.join(" "));
            w.set_env("SIMPLERAG_INDEX_EXCLUDE", &self.s.exclude_folders.join(";"));
            if w.availability() != "down" {
                w.unload(why);
                w.start();
                w.ensure_started();
                restart = true;
            }
        }
        let mut msg = "저장했습니다".to_string();
        match self.jobs.as_mut() {
            None => {}
            Some(j) => {
                j.filter_changed(&self.s, approve, !restart);
                msg += if !j.enabled {
                    " — 자동 인덱싱이 꺼져 있어 켜면 반영합니다"
                } else if restart {
                    " — 모델을 다시 올린 뒤 폴더에 반영합니다 (10초 안팎)"
                } else {
                    " — 폴더에 반영합니다"
                };
            }
        }
        self.update_tooltip();
        msg
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
        self.save_browse_place();
        self.info.window.set_visible(false);
        crate::info!("app", "종료 완료");
    }

    // 탐색기 창 자리 저장 — INI [UI] BrowseWindow (바뀌었을 때만)
    fn save_browse_place(&mut self) {
        let Some(t) = self.browse.place_text() else { return };
        if t == self.s.browse_window {
            return;
        }
        if let Err(e) = settings::save_ini_value(&self.s.ini_path, "UI", "BrowseWindow", &t) {
            crate::warn!("browse", "탐색기 창 자리를 저장하지 못했다: {}", e);
        }
        self.s.browse_window = t;
    }
}

//------------------------------------------------------------------
// --autorun on|off|status — 부모 콘솔에 결과를 쓰고(없으면 알림 창) 끝낸다
//   --quiet 를 붙이면 알림 창을 띄우지 않는다 — 설치 · 제거 프로그램이 부를 때 창이 절차를 멈추지 않게
//------------------------------------------------------------------
fn autorun_cli(action: &str) -> i32 {
    use windows::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
    let attached = unsafe { AttachConsole(ATTACH_PARENT_PROCESS) }.is_ok();
    let quiet = std::env::args().any(|a| a == "--quiet");
    let out = |text: &str, modal: bool| {
        crate::info!("autorun", "{}", text);
        if attached {
            if let Ok(mut f) = std::fs::OpenOptions::new().write(true).open("CONOUT$") {
                use std::io::Write;
                let _ = writeln!(f, "{}", text);
            }
        } else if modal && !quiet {
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
            out("사용법: MoAISearch.exe --autorun on|off|status", false);
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
    crate::info!("main", "MoAISearch 시작 — 설정 {}", s.ini_path);
    watcher::set_exts(&s.index_exts);

    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy: Proxy = Arc::new(Mutex::new(event_loop.create_proxy()));
    let icon = Icon::from_resource(1, None).ok();

    // WebView2 사용자 데이터는 %LOCALAPPDATA%\RAGSearchBox-rs\webview (exe 옆이 쓰기 금지여도 되게)
    //   로그 폴더(MoAISearch\log)와 따로 둔다 — 옮기면 캐시를 새로 만들게 된다
    let data = std::path::Path::new(&log::app_dir()).join("webview");
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
    let p1d = proxy.clone();
    let pview = WebViewBuilder::with_web_context(ctx).with_html(html)
        .with_ipc_handler(move |req| post(&p1, UserEvent::PanelIpc(req.into_body())))
        // 끌어 놓기 — 경로를 이벤트 루프로 넘긴다. 처리기를 달면 WebView2 기본 동작(놓은 파일로 화면 이동)은 꺼진다
        .with_drag_drop_handler(move |e| { post(&p1d, UserEvent::PanelDrop(e)); true })
        .build(&pwin).expect("패널 화면");

    // 설정 창
    let iwin = WindowBuilder::new().with_title("MoAI-S 설정").with_visible(false).with_window_icon(icon)
        .with_resizable(false).with_inner_size(tao::dpi::LogicalSize::new(INFO_W, INFO_H)).build(&event_loop).expect("설정 창");
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
    let p3d = proxy.clone();
    let bview = WebViewBuilder::with_web_context(unsafe { &mut *(ctx as *mut WebContext) })
        .with_html(BROWSE_HTML.replace("__FONT_PX__", &font).replace("__MOAI__", &moai).replace("__LAYOUT__", "window").replace("__UISTATE__", &settings::ui_state_js(&s.browse_state)))
        .with_ipc_handler(move |req| post(&p3, UserEvent::BrowseIpc(req.into_body())))
        // 끌어 놓기 — 비슷한 문서 찾기 입력란(화면이 좌표로 입력란 위인지 판단). 기본 동작(놓은 파일로 화면 이동)은 끈다
        .with_drag_drop_handler(move |e| { post(&p3d, UserEvent::BrowseDrop(e)); true })
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
            let w = Worker::new(cmd.clone(), vec![("SIMPLERAG_RELEVANCE_MIN".into(), s.relevance_min.clone()), ("SIMPLERAG_EVIDENCE_FULL".into(), "1".into()),
                                                  ("SIMPLERAG_INDEX_EXTS".into(), s.index_exts.join(" ")),
                                                  ("SIMPLERAG_INDEX_EXCLUDE".into(), s.exclude_folders.join(";"))],
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
    // 탐색기 감시는 늘 켠다 — [나란히 보기]([Panel] Enabled)를 실행 중에 켜고 끌 수 있게(끄면 on_folder 가 무시한다)
    let monitor = Monitor::start(scope.clone(), s.folder_poll_ms, true, Arc::new(move |e| post(&pm, UserEvent::Monitor(e))));
    let history_path = history::default_path();
    // 지난 탐색기 창 자리 — s 는 App 으로 옮겨지므로 먼저 읽어 둔다
    let browse_place = Browse::parse_place(&s.browse_window);
    let cand_state = candidates::CandState::parse(&s.cand_state);
    let mut app = App {
        panel: Panel::new(pwin, pview, &s),
        info: InfoWin { window: iwin, webview: iview, visible: false, ready: false, action_msg: String::new(), folder_msg: String::new(),
                       syn_msg: String::new(), syn_done: Arc::new(Mutex::new(None)), syn_running: Arc::new(std::sync::atomic::AtomicBool::new(false)),
                       ppl_msg: String::new(), ppl_done: Arc::new(Mutex::new(None)), ppl_running: Arc::new(std::sync::atomic::AtomicBool::new(false)),
                       prec_msg: String::new(), mt_msg: String::new(), ext_msg: String::new() },
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
        roots_ver: 0,
        t_roots: Instant::now(),
        t_info: Instant::now(),
        cand: cand_state,
        // 처음 점검은 뜬 지 10초 뒤 — [⚙ 설정] 숫자가 곧 보이게
        t_cand: Instant::now().checked_sub(Duration::from_secs(50)).unwrap_or_else(Instant::now),
        t_user: Instant::now(),
        cand_n: usize::MAX,
        quitting: false,
        selftest_done: false,
        up_done: Arc::new(Mutex::new(vec![])),
        up_running: 0,
        browse: {
            // 지난 창 자리 — 처음 보일 때 쓴다
            let mut b = Browse::new(bwin, bview, pv);
            b.place = browse_place;
            b
        },
        panel_mode: String::new(),
        ask_to_browse: false,
        find_done: Arc::new(Mutex::new(vec![])),
        icon_msg: String::new(),
        mode_msg: String::new(),
        first_folder_asked: false,
        load_shown: false,
        t_load: Instant::now(),
        moai_b64: moai,
    };
    app.panel_mode = app.s.ui_mode.clone();
    // 📌 항상 위 — 지난 값(기본 켬)을 창에 적용. 숨긴 창에도 걸어 두면 보일 때 그대로다
    win::set_topmost(app.panel.hwnd(), app.s.panel_on_top);
    win::set_topmost(app.browse.hwnd(), app.s.browse_on_top);
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
            // 탐색기 창을 배율이 다른 모니터로 옮겼다 — PDF 미리 보기 자리를 새 배율로
            Event::WindowEvent { window_id, event: WindowEvent::ScaleFactorChanged { .. }, .. } if window_id == browse_id => {
                app.browse.dpi_changed();
            }
            Event::WindowEvent { window_id, event: WindowEvent::CloseRequested, .. } => {
                if window_id == panel_id {
                    app.panel.close_by_user();
                } else if window_id == info_id {
                    app.info.visible = false;
                    app.info.window.set_visible(false);
                } else if window_id == browse_id {
                    // 닫기 = 숨기기(트레이에 남는다) · 창 자리를 기억한다
                    app.browse.hide();
                    app.save_browse_place();
                }
            }
            _ => {}
        }
        if app.quitting {
            *flow = ControlFlow::Exit;
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    // [+] · 끌어 놓기 공통 검사 — 폴더 · 형식 · 크기 · 같은 이름 · 개수 순으로 막는다
    #[test]
    fn upload_reject_rules() {
        let mb = 1024 * 1024;
        assert!(upload_reject(false, "pdf", 10 * mb, 0, false).is_none());
        assert!(upload_reject(true, "", 0, 0, false).unwrap().contains("폴더"));
        assert!(upload_reject(false, "exe", 1, 0, false).unwrap().contains("형식"));
        let big = upload_reject(false, "pdf", 42 * mb + mb / 3, 0, false).unwrap();
        assert!(big.starts_with("42.3MB") && big.contains("30MB"), "{}", big);
        assert!(upload_reject(false, "pdf", 1, 0, true).unwrap().contains("같은 이름"));
        assert!(upload_reject(false, "pdf", 1, UPLOAD_MAX_FILES - 1, false).is_none());
        assert!(upload_reject(false, "pdf", 1, UPLOAD_MAX_FILES, false).unwrap().contains("5개"));
    }

    #[test]
    fn str_list_skips_non_strings() {
        assert_eq!(str_list(&json!(["a", 1, "b"])), vec!["a", "b"]);
        assert!(str_list(&json!(null)).is_empty());
    }
}
