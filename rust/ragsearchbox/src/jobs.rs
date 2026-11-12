//------------------------------------------------------------------
// 자동 인덱싱 (Python index_jobs.py)
//=> 폴더 감시 묶음을 받아 "대조 → 문서별 반영 → 키워드 색인" 을 워커에게 하나씩 시킨다.
//    · 워커가 떠 있으면: /index-outside → /index-plan → /index-doc·/remove-doc → /bm25
//    · 워커가 내려가 있으면: `simplerag-rs index --dir` 를 따로 돌린다(그동안 워커를 띄우지 않는다)
//    · 새 문서가 너무 많거나(AskAboveDocs) 한꺼번에 많이 사라지면 멈추고 [예] · [아니요] 창으로 허락을 묻는다
//      (아니요면 트레이 · 폴더 오른쪽 클릭 '지금 인덱싱' 을 기다린다)
//   메인 스레드에서만 돈다(tick). 워커·명령 스레드의 결과는 post 로 메인에 돌려받는다.
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::settings::Settings;
use crate::worker::Worker;

const SUMMARY_PREFIX: &str = "@index-summary ";
const CLI_RETRY_S: u64 = 60;
const NOTIFY_AT_LEAST: usize = 3;
// 벡터 채우기는 마지막 질문 뒤 이만큼 쉬었을 때만 보낸다(질문이 먼저 — 인덱싱 속도 개선 설계서 §3-5-5)
const PENDING_IDLE_S: f64 = 2.0;
// 한 번에 채울 벡터 대기 청크 수 — 4스레드로 약 1초라 질문이 오면 1초 안에 비켜 준다
const PENDING_STEP: usize = 64;
// 이만큼 넘게 채울 때만 진행 · 완료를 알린다 — 문서 한 건 저장으로 상태 줄이 깜박이지 않게
const VEC_SHOW_MIN: usize = 500;
// 라운드 도중 키워드 색인 반영 주기 — 문서 수나 시간 중 먼저 닿는 쪽(벡터 백그라운드 설계서 §7-2)
const BM25_MID_DOCS: usize = 20;
const BM25_MID_S: u64 = 30;
// 중간 반영에 든 시간의 이 배수만큼은 쉰다 — 덧붙임이 커져도 반영이 문서 처리를 잡아먹지 않게
const BM25_MID_REST: u32 = 20;
const OUTSIDE_KEY: &str = "(지정 폴더 밖)";

//------------------------------------------------------------------
// 메인 스레드로 돌아오는 결과
//------------------------------------------------------------------
pub enum JobMsg {
    Done(u64, Value),
    Cli(Vec<(String, Option<i32>, Option<Value>, String, f64)>),
    // index 명령의 진행 — (폴더, 끝낸 수, 전체)
    CliProgress(String, usize, usize),
}

// 보낸 명령의 결과를 받을 곳
enum Handler {
    Outside,
    Plan(String),
    Doc(String, String, Option<String>),
    Bm25,
    Bm25Mid,
    Pending,
}

struct Round {
    done: usize,
    total: usize,
    changed: Vec<String>,
    failed: Vec<String>,
}

//------------------------------------------------------------------
// 자동 인덱싱 상태
//------------------------------------------------------------------
pub struct IndexJobs {
    s: Settings,
    worker: Option<Arc<Worker>>,
    pub roots: Vec<String>,
    base_cmd: Vec<String>,
    post: Arc<dyn Fn(JobMsg) + Send + Sync>,
    notify: Box<dyn Fn(&str, &str)>,
    pub enabled: bool,
    pub disabled_reason: Option<String>,
    dirty: Vec<(String, String)>,
    approved: HashSet<String>,
    docs: VecDeque<(String, String, Option<String>)>,
    queued: HashSet<(String, String)>,
    inflight: Option<String>,
    handlers: HashMap<u64, Handler>,
    next_id: u64,
    bm25_needed: bool,
    // 마지막 키워드 색인 반영 뒤 처리한 문서 수 · 반영 시각 · 지난 중간 반영에 든 시간
    bm25_mid_docs: usize,
    bm25_mid_at: Instant,
    bm25_mid_cost: Duration,
    retry_at: Option<Instant>,
    last_avail: Option<&'static str>,
    outside_needed: bool,
    approve_outside: bool,
    pub waiting_new: HashMap<String, usize>,
    pub waiting_delete: HashMap<String, String>,
    round: Round,
    // 벡터 대기 청크(단계형 인덱싱) — None 이면 아직 모름(워커가 뜨면 한 번 물어본다)
    vec_pending: Option<usize>,
    // 이번에 채우기 시작할 때의 대기 수(진행 표시 "12,345 / 186,919" 의 분모)
    vec_total: usize,
    // 이름이 바뀌어 쉬는 동안 제목을 다시 만들 문서 수(워커 /embed-pending 이 한 건씩 처리)
    retitle_pending: usize,
    // 폴더별 인덱싱 진행 — 폴더 → (끝낸 수, 전체). 탐색 창에 "Desktop (10/311)" 로 보이고, 끝나면 뺀다
    prog: HashMap<String, (usize, usize)>,
    // 진행 · 대기가 바뀔 때마다 늘어난다 — 메인이 보고 탐색 창 숫자를 고친다
    pub prog_ver: u64,
    // 이번 차례에 허락받고 도는 폴더 — 워커가 도중에 내려가도 다시 묻지 않고 이어 간다
    running_approved: HashSet<String>,
}

fn abspath(p: &str) -> String {
    std::path::absolute(p).map(|x| x.to_string_lossy().into_owned()).unwrap_or(p.to_string())
}
// 세 자리마다 쉼표(12,345)
fn comma(n: usize) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}
fn basename(p: &str) -> String {
    p.rsplit(['\\', '/']).next().unwrap_or(p).to_string()
}

impl IndexJobs {
    pub fn new(s: &Settings, worker: Option<Arc<Worker>>, roots: &[String], base_cmd: &[String],
               post: Arc<dyn Fn(JobMsg) + Send + Sync>, notify: Box<dyn Fn(&str, &str)>) -> IndexJobs {
        IndexJobs {
            s: s.clone(), worker, roots: roots.iter().map(|r| abspath(r)).collect(), base_cmd: base_cmd.to_vec(), post, notify,
            enabled: s.autoindex_enabled, disabled_reason: None, dirty: vec![], approved: HashSet::new(), docs: VecDeque::new(),
            queued: HashSet::new(), inflight: None, handlers: HashMap::new(), next_id: 1, bm25_needed: false, bm25_mid_docs: 0, bm25_mid_at: Instant::now(), bm25_mid_cost: Duration::ZERO, retry_at: None,
            last_avail: None, outside_needed: true, approve_outside: false, waiting_new: HashMap::new(), waiting_delete: HashMap::new(),
            round: Round { done: 0, total: 0, changed: vec![], failed: vec![] },
            vec_pending: None, vec_total: 0, retitle_pending: 0, prog: HashMap::new(), prog_ver: 0, running_approved: HashSet::new(),
        }
    }

    // 벡터 채우기 진행 (채운 수, 전체) — 대기 청크가 없거나 소량이면 None
    fn vec_progress(&self) -> Option<(usize, usize)> {
        let left = self.vec_pending.filter(|n| *n > 0)?;
        let total = self.vec_total.max(left);
        (total >= VEC_SHOW_MIN).then_some((total - left, total))
    }

    // 벡터 채우기 진행 글 — 보일 것이 없으면 None
    pub fn pending_text(&self) -> Option<String> {
        let (done, total) = self.vec_progress()?;
        Some(format!("검색 정확도 높이는 중 {} / {} — 지금도 검색할 수 있습니다", comma(done), comma(total)))
    }

    //------------------------------------------------------------------
    // 감시 묶음 받기 — 그 폴더를 대조 대상으로
    //------------------------------------------------------------------
    pub fn on_batch(&mut self, b: &crate::watcher::Batch) {
        let why = if b.rescan { b.reason.clone() } else { format!("변경 {}·삭제 {}", b.changed.len(), b.removed.len()) };
        crate::diag!("autoindex", "감시 묶음: {} ({})", b.root, why);
        self.mark_dirty(&b.root.clone(), &why);
    }

    pub fn mark_dirty(&mut self, root: &str, why: &str) {
        let r = abspath(root);
        if !self.dirty.iter().any(|(x, _)| *x == r) {
            self.dirty.push((r, why.to_string()));
        }
    }

    //------------------------------------------------------------------
    // 사용자가 고른 폴더를 바로 인덱싱 — 폴더 추가 · 폴더 오른쪽 클릭 '지금 인덱싱'
    //=> 사람이 직접 고른 것 자체가 허락이다. 새 문서 수 한도(AskAboveDocs)는 감시 중에 문서가 뜻밖에
    //   몰려들 때를 막으려는 것이라 여기서는 묻지 않는다.
    //------------------------------------------------------------------
    pub fn approve_folder(&mut self, root: &str, why: &str) {
        let r = abspath(root);
        crate::info!("autoindex", "{} — 바로 인덱싱: {}", why, r);
        self.approved.insert(r.clone());
        self.mark_dirty(&r, why);
    }

    // 이 폴더가 대량 삭제 허락을 기다리는가
    pub fn root_delete_wait(&self, root: &str) -> bool {
        let low = root.to_lowercase();
        self.waiting_delete.keys().any(|k| k.to_lowercase() == low)
    }

    //------------------------------------------------------------------
    // 탐색 창에 보일 폴더 상태 — (진행 중이면 Some((끝낸 수, 전체)), 허락을 기다리는 새 문서 수)
    //------------------------------------------------------------------
    pub fn root_state(&self, root: &str) -> (Option<(usize, usize)>, usize) {
        let low = root.to_lowercase();
        let prog = self.prog.iter().find(|(k, _)| k.to_lowercase() == low).map(|(_, v)| *v);
        let wait = self.waiting_new.iter().find(|(k, _)| k.to_lowercase() == low).map(|(_, n)| *n).unwrap_or(0);
        (prog, wait)
    }

    //------------------------------------------------------------------
    // 지금 인덱싱이 진행 중인가 — 검색 결과가 비었을 때 "없다" 와 "아직 안 들어왔다" 를 가르려고 쓴다
    //=> 처리 중이면 Some((끝낸 수, 전체)), 대조를 기다리거나 도는 중이면 Some((0, 0)), 아니면 None
    //------------------------------------------------------------------
    pub fn index_stage(&self) -> Option<(usize, usize)> {
        if !self.enabled {
            return None;
        }
        if !self.prog.is_empty() {
            return Some(self.prog.values().fold((0, 0), |a, v| (a.0 + v.0, a.1 + v.1)));
        }
        let scanning = self.inflight.as_deref().map(|s| s.starts_with("대조")).unwrap_or(false);
        if scanning || !self.dirty.is_empty() { Some((0, 0)) } else { None }
    }

    //------------------------------------------------------------------
    // 설정 > 로그 판 [다시 시도] — 실패했던 문서 한 건을 큐에 넣는다(로그 보기 설계서 §4-3)
    //=> 같은 파일로 실패했던 문서는 워커가 건너뛰므로(failed_before) retry 표시를 붙여 보낸다.
    //   index 명령(CLI)은 실패 문서를 다시 하지 않으므로 워커가 올라와 있을 때만 받는다.
    // -in: path = 문서 경로
    // -out: Ok(안내 글) / 받을 수 없으면 Err(까닭)
    //------------------------------------------------------------------
    pub fn retry_doc(&mut self, path: &str) -> Result<String, String> {
        if !self.enabled {
            return Err("자동 인덱싱이 꺼져 있어 다시 시도할 수 없습니다 — 트레이에서 켜 주세요".into());
        }
        if let Some(r) = &self.disabled_reason {
            return Err(r.clone());
        }
        if self.worker.as_ref().map(|w| w.availability()).unwrap_or("down") != "up" {
            return Err("모델이 올라와 있을 때 다시 시도할 수 있습니다 — 질문을 한 번 하거나 설정 > 기본에서 모델을 올려 주세요".into());
        }
        let p = abspath(path);
        // 같은 문서를 두 번 넣지 않는다
        if self.queued.insert(("retry-doc".into(), p.to_lowercase())) {
            self.docs.push_back(("retry-doc".into(), p.clone(), None));
            self.round.total += 1;
        }
        crate::info!("autoindex", "다시 시도 요청: {}", p);
        Ok(format!("다시 시도합니다: {}", basename(&p)))
    }

    pub fn rescan_all(&mut self) {
        for r in self.roots.clone() {
            self.mark_dirty(&r, "사용자 요청");
        }
    }

    //------------------------------------------------------------------
    // 인덱싱 대상(확장자 · 뺀 폴더)이 바뀜
    //   approve = 다음 대조를 허락받은 것으로 칠까. 확장자는 사용자가 고른 일이라 AskAboveDocs 로 멈추지 않게 true,
    //             하위 폴더 빼기는 기다리던 새 문서까지 들어가면 안 되므로 false
    //   rescan = 지금 대조를 부탁할까. 워커를 다시 띄우면 뜰 때 대조하므로 false 로 부른다
    //   (워커를 띄우는데 여기서 부탁하면 뜨기 전 잠깐 사이에 index 명령이 먼저 돈다)
    //------------------------------------------------------------------
    pub fn filter_changed(&mut self, s: &Settings, approve: bool, rescan: bool) {
        self.s.index_exts = s.index_exts.clone();
        self.s.exclude_folders = s.exclude_folders.clone();
        for r in self.roots.clone() {
            if approve {
                self.approved.insert(r.clone());
            }
            if rescan {
                self.mark_dirty(&r, "인덱싱 대상 변경");
            }
        }
    }

    //------------------------------------------------------------------
    // 트레이 '지금 인덱싱' — 기다리던 폴더(없으면 전부)를 허락하고 대조
    //------------------------------------------------------------------
    pub fn run_now(&mut self) -> usize {
        let mut roots: HashSet<String> = self.waiting_new.keys().cloned().chain(self.waiting_delete.keys().cloned()).collect();
        if roots.remove(OUTSIDE_KEY) {
            self.approve_outside = true;
            self.outside_needed = true;
        }
        if roots.is_empty() && !self.approve_outside {
            roots = self.roots.iter().cloned().collect();
        }
        let mut v: Vec<String> = roots.into_iter().collect();
        v.sort();
        for r in &v {
            self.approved.insert(r.clone());
            self.mark_dirty(r, "사용자 허락");
        }
        crate::info!("autoindex", "지금 인덱싱: {}", v.join(", "));
        v.len()
    }

    pub fn set_enabled(&mut self, on: bool) {
        self.enabled = on;
        if on {
            self.disabled_reason = None;
            self.rescan_all();
        }
    }

    //------------------------------------------------------------------
    // 트레이 툴팁에 붙일 글
    //------------------------------------------------------------------
    pub fn status_text(&self) -> String {
        if !self.enabled {
            return "자동 인덱싱 꺼짐".into();
        }
        if let Some(r) = &self.disabled_reason {
            return format!("자동 인덱싱 멈춤 — {}", r);
        }
        if self.inflight.is_some() || !self.docs.is_empty() {
            if self.round.total > 0 {
                return format!("문서 처리 중 {}/{}", self.round.done, self.round.total);
            }
            return "인덱스 확인 중".into();
        }
        if !self.waiting_new.is_empty() {
            return format!("새 문서 {}건 — 트레이 '지금 인덱싱'", self.waiting_new.values().sum::<usize>());
        }
        if !self.waiting_delete.is_empty() {
            return "사라진 문서가 많아 멈춤 — 트레이 '지금 인덱싱'".into();
        }
        self.pending_text().unwrap_or_default()
    }

    pub fn has_waiting(&self) -> bool {
        self.enabled && self.disabled_reason.is_none() && (!self.waiting_new.is_empty() || !self.waiting_delete.is_empty())
    }

    // 메뉴 맨 위 줄에 붙일 짧은 글
    pub fn short_state(&self) -> String {
        if !self.enabled {
            return String::new();
        }
        if self.disabled_reason.is_some() {
            return "자동 인덱싱 멈춤".into();
        }
        if self.inflight.is_some() || !self.docs.is_empty() {
            if self.round.total > 0 {
                return format!("문서 처리 중 {}/{}", self.round.done, self.round.total);
            }
            return "인덱스 확인 중".into();
        }
        if !self.waiting_new.is_empty() || !self.waiting_delete.is_empty() {
            return "'지금 인덱싱' 필요".into();
        }
        match self.vec_progress() {
            Some((done, total)) => format!("검색 정확도 높이는 중 {}/{}", comma(done), comma(total)),
            None => String::new(),
        }
    }

    //------------------------------------------------------------------
    // 한 걸음 — 메인 스레드가 자주 부른다
    //------------------------------------------------------------------
    pub fn tick(&mut self) {
        if !self.enabled || self.disabled_reason.is_some() || self.inflight.is_some() {
            return;
        }
        let avail = self.worker.as_ref().map(|w| w.availability()).unwrap_or("down");
        if avail == "up" && self.last_avail != Some("up") {
            for r in self.roots.clone() {
                self.mark_dirty(&r, "워커가 떴다");
            }
            self.outside_needed = true;
            // 남은 벡터 대기 청크가 있는지 다시 본다(지난번에 채우다 끊겼을 수 있다)
            self.vec_pending = None;
        }
        self.last_avail = Some(avail);
        if avail == "starting" {
            return;
        }
        if avail == "up" {
            self.tick_worker();
        } else if (!self.dirty.is_empty() || !self.docs.is_empty()) && self.retry_at.map(|t| Instant::now() >= t).unwrap_or(true) {
            self.run_cli();
        } else if self.round_active() {
            self.finish_round();
        }
    }

    fn tick_worker(&mut self) {
        if self.outside_needed {
            self.outside_needed = false;
            let line = format!("/index-outside {}", crate::worker::ascii_json(&json!({"roots": self.roots})));
            self.send(line, Handler::Outside, "지정 폴더 밖 문서 확인");
            return;
        }
        // 문서가 많을 때 — 끝까지 기다리면 처리한 문서가 어디서도 안 찾히므로 도중에 키워드 색인에 넣는다
        if self.bm25_mid_due() {
            self.send("/bm25 {\"merge\": false}".into(), Handler::Bm25Mid, "키워드 색인 중간 반영");
            return;
        }
        if let Some((op, path, root)) = self.docs.pop_front() {
            self.queued.remove(&(op.clone(), path.to_lowercase()));
            let line = if op == "remove-outside" {
                format!("/remove-doc {}", crate::worker::ascii_json(&json!({"path": path, "outside_of": self.roots})))
            } else if op == "retry-doc" {
                // 다시 시도 — 실패했던 같은 파일이어도 건너뛰지 말라고 retry 를 붙인다
                format!("/index-doc {}", crate::worker::ascii_json(&json!({"path": path, "retry": true})))
            } else {
                format!("/{} {}", op, crate::worker::ascii_json(&json!({"path": path})))
            };
            let label = format!("{} {}", op, basename(&path));
            self.send(line, Handler::Doc(op, path, root), &label);
            return;
        }
        if !self.dirty.is_empty() {
            let (root, why) = self.dirty.remove(0);
            let mut arg = json!({"path": root});
            if self.approved.contains(&root) {
                arg["allow_delete"] = json!(true);
            }
            crate::info!("autoindex", "폴더 대조: {} ({})", root, why);
            let label = format!("대조 {}", root);
            self.send(format!("/index-plan {}", crate::worker::ascii_json(&arg)), Handler::Plan(root), &label);
            return;
        }
        if self.bm25_needed {
            let idle = self.worker.as_ref().map(|w| w.seconds_since_ask()).unwrap_or(0.0);
            if idle >= self.s.autoindex_bm25_idle_s as f64 {
                self.send("/bm25".into(), Handler::Bm25, "키워드 색인");
            }
            return;
        }
        if self.round_active() {
            self.finish_round();
        }
        // 벡터 대기 청크 채우기 — 질문을 처리하지 않을 때만, 64개씩(질문이 오면 1초 안에 비켜 준다)
        let idle = self.worker.as_ref().map(|w| w.is_idle() && w.seconds_since_ask() >= PENDING_IDLE_S).unwrap_or(false);
        if idle && (self.vec_pending.map(|n| n > 0).unwrap_or(true) || self.retitle_pending > 0) {
            let line = format!("/embed-pending {}", crate::worker::ascii_json(&json!({"max": PENDING_STEP})));
            self.send(line, Handler::Pending, "벡터 채우기");
        }
    }

    // 명령 보내기 — 결과는 post 로 돌아온다
    fn send(&mut self, line: String, h: Handler, label: &str) {
        self.inflight = Some(label.to_string());
        let id = self.next_id;
        self.next_id += 1;
        self.handlers.insert(id, h);
        let post = self.post.clone();
        if let Some(w) = &self.worker {
            w.command(line, Box::new(move |res| post(JobMsg::Done(id, res))));
        }
    }

    //------------------------------------------------------------------
    // 결과 받기(메인 스레드)
    //------------------------------------------------------------------
    pub fn on_msg(&mut self, m: JobMsg) {
        match m {
            JobMsg::Done(id, res) => {
                self.inflight = None;
                let h = match self.handlers.remove(&id) {
                    Some(h) => h,
                    None => return,
                };
                if res.get("unknown").and_then(|v| v.as_bool()).unwrap_or(false) {
                    let r = "simplerag-rs.exe 가 인덱싱 명령을 모릅니다(새로 빌드 필요)".to_string();
                    crate::warn!("autoindex", "{} — 출력: {}", r, res["raw"].as_str().unwrap_or(""));
                    (self.notify)("warn", &r);
                    self.disabled_reason = Some(r);
                    return;
                }
                match h {
                    Handler::Outside => self.on_outside(&res),
                    Handler::Plan(root) => self.on_plan(&root, &res),
                    Handler::Doc(op, path, root) => self.on_doc(&op, &path, root, &res),
                    Handler::Bm25 => self.on_bm25(&res),
                    Handler::Bm25Mid => self.on_bm25_mid(&res),
                    Handler::Pending => self.on_pending(&res),
                }
            }
            JobMsg::Cli(results) => self.on_cli(results),
            JobMsg::CliProgress(root, n, total) => {
                self.prog.insert(root, (n, total));
                self.prog_ver += 1;
            }
        }
    }

    fn b(res: &Value, k: &str) -> bool {
        res.get(k).and_then(|v| v.as_bool()).unwrap_or(false)
    }
    fn strs(res: &Value, k: &str) -> Vec<String> {
        res.get(k).and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or_default()
    }

    fn on_plan(&mut self, root: &str, res: &Value) {
        if Self::b(res, "stopped") {
            self.mark_dirty(root, "워커가 도중에 내려감");
            return;
        }
        if !Self::b(res, "ok") {
            crate::warn!("autoindex", "폴더 대조 실패: {} — {}", root, res["why"]);
            return;
        }
        let approved = self.approved.remove(root);
        if approved {
            self.running_approved.insert(root.to_string());
        }
        self.prog_ver += 1;
        let mut add = Self::strs(res, "add");
        let modify = Self::strs(res, "modify");
        let remove = Self::strs(res, "remove");
        let mv = Self::strs(res, "move");
        let limit = self.s.autoindex_ask_above as usize;
        if limit > 0 && add.len() > limit && !approved {
            if self.waiting_new.get(root) != Some(&add.len()) {
                (self.notify)("ask", &format!("새 문서 {}건이 있습니다", add.len()));
            }
            self.waiting_new.insert(root.into(), add.len());
            crate::info!("autoindex", "새 문서 {}건 > {} — 허락을 기다린다: {}", add.len(), limit, root);
            add.clear();
        } else {
            self.waiting_new.remove(root);
        }
        let db = res["delete_blocked"].as_str().unwrap_or("").to_string();
        if !db.is_empty() {
            if !self.waiting_delete.contains_key(root) {
                (self.notify)("ask", &format!("{}\n정말 지운 문서라면 인덱스에서도 뺍니다.", db));
            }
            self.waiting_delete.insert(root.into(), db);
        } else {
            self.waiting_delete.remove(root);
        }
        let idx: Vec<String> = mv.iter().chain(modify.iter()).chain(add.iter()).cloned().collect();
        for (op, paths) in [("remove-doc", &remove), ("index-doc", &idx)] {
            for p in paths {
                let key = (op.to_string(), p.to_lowercase());
                if self.queued.insert(key) {
                    self.docs.push_back((op.into(), p.clone(), Some(root.into())));
                    self.round.total += 1;
                    self.prog.entry(root.to_string()).or_default().1 += 1;
                }
            }
        }
        if Self::b(res, "bm25_stale") {
            self.bm25_needed = true;
        }
        if !add.is_empty() || !modify.is_empty() || !remove.is_empty() || !mv.is_empty() {
            crate::info!("autoindex", "대조 결과 {}: 추가 {} · 수정 {} · 이동 {} · 삭제 {}", root, add.len(), modify.len(), mv.len(), remove.len());
        }
    }

    fn on_outside(&mut self, res: &Value) {
        if Self::b(res, "stopped") {
            self.outside_needed = true;
            return;
        }
        if !Self::b(res, "ok") {
            crate::warn!("autoindex", "지정 폴더 밖 문서 확인 실패: {}", res["why"]);
            return;
        }
        let out = Self::strs(res, "outside");
        let approved = std::mem::replace(&mut self.approve_outside, false);
        if out.is_empty() {
            self.waiting_delete.remove(OUTSIDE_KEY);
            return;
        }
        let db = res["delete_blocked"].as_str().unwrap_or("").to_string();
        if !db.is_empty() && !approved {
            if !self.waiting_delete.contains_key(OUTSIDE_KEY) {
                (self.notify)("ask", &format!("지정 폴더 밖 문서 {}건이 인덱스에 있습니다({})\n인덱스에서 뺍니다.", out.len(), db));
            }
            self.waiting_delete.insert(OUTSIDE_KEY.into(), db);
            crate::info!("autoindex", "지정 폴더 밖 문서 {}건 — 허락을 기다린다", out.len());
            return;
        }
        self.waiting_delete.remove(OUTSIDE_KEY);
        crate::info!("autoindex", "지정 폴더 밖 문서 {}건을 인덱스에서 뺀다(인덱싱 폴더 = 패널 폴더)", out.len());
        for p in out {
            if self.queued.insert(("remove-outside".into(), p.to_lowercase())) {
                self.docs.push_back(("remove-outside".into(), p, None));
                self.round.total += 1;
            }
        }
    }

    fn on_doc(&mut self, op: &str, path: &str, root: Option<String>, res: &Value) {
        if Self::b(res, "stopped") {
            match root {
                None => self.outside_needed = true,
                Some(r) => {
                    // 허락받고 돌던 폴더는 허락을 되살린다 — 다시 대조할 때 한도에 걸려 멈추지 않게
                    if self.running_approved.contains(&r) {
                        self.approved.insert(r.clone());
                    }
                    self.mark_dirty(&r, "워커가 도중에 내려감");
                }
            }
            return;
        }
        self.round.done += 1;
        // 벡터를 미룬 문서 — 차례가 끝나면 /embed-pending 으로 채운다
        //   이름이 바뀐 큰 문서도 — 쉬는 동안 제목을 다시 만든다(같은 명령이 처리)
        if res["deferred"].as_u64().unwrap_or(0) > 0 || Self::b(res, "retitle_later") {
            self.vec_pending = None;
        }
        // 폴더별 진행 — 다 끝난 폴더는 빼서 문서 수만 보이게 한다
        if let Some(p) = root.as_ref().and_then(|r| self.prog.get_mut(r)) {
            p.0 += 1;
            if p.0 >= p.1 {
                let r = root.as_ref().unwrap();
                self.prog.remove(r);
            }
            self.prog_ver += 1;
        }
        let result = res["result"].as_str().unwrap_or("");
        let name = basename(path);
        if Self::b(res, "ok") && result == "moved" {
            self.round.changed.push(name.clone());
            crate::info!("autoindex", "반영 이동: {} → {}", res["from"].as_str().unwrap_or("?"), path);
            (self.notify)("doc", &format!("방금 반영(이동): {}", name));
        } else if Self::b(res, "ok") && ["added", "modified", "copied", "removed", "retitled"].contains(&result) {
            self.bm25_needed = true;
            self.bm25_mid_docs += 1;
            self.round.changed.push(name.clone());
            // retitled = 이름이 바뀐 문서를 옮기고 새 제목으로 다시 청킹했다
            let label = match result { "added" => "추가", "modified" => "수정", "copied" => "복사", "retitled" => "이름 바꿈", _ => "삭제" };
            crate::info!("autoindex", "반영 {}: {} ({}청크, {}ms)", label, path, res.get("chunks").or(res.get("removed")).cloned().unwrap_or(json!("")), res["ms"]);
            (self.notify)("doc", &format!("방금 반영({}): {}", label, name));
        } else if !Self::b(res, "ok") {
            // 사람이 [다시 시도]를 눌렀으면 결과를 바로 알린다(감시 반영 실패는 차례 끝 요약으로만)
            if op == "retry-doc" {
                (self.notify)("warn", &format!("다시 시도했지만 실패: {} — {}", name, res["why"].as_str().unwrap_or("")));
            }
            self.round.failed.push(name.clone());
            crate::warn!("autoindex", "반영 실패 {}: {} — {}", op, path, res["why"]);
        }
    }

    //------------------------------------------------------------------
    // 벡터 채우기 결과 — 남은 수를 기억하고, 다 채우면 한 번 알린다
    //------------------------------------------------------------------
    fn on_pending(&mut self, res: &Value) {
        if Self::b(res, "stopped") {
            return;
        }
        if !Self::b(res, "ok") {
            // 실패하면 이번 워커에서는 더 보내지 않는다(다음에 워커가 뜨면 다시 본다)
            crate::warn!("autoindex", "벡터 채우기 실패: {}", res["why"]);
            self.vec_pending = Some(0);
            return;
        }
        // 제목 갱신 — 남은 수를 기억하고, 이번에 한 건 했으면 키워드 색인도 다시(청크 글이 바뀜)
        self.retitle_pending = res["retitle"].as_u64().unwrap_or(0) as usize;
        if res["retitled"].as_u64().unwrap_or(0) > 0 {
            crate::info!("autoindex", "제목 갱신 1건 — 남은 {}건", self.retitle_pending);
            self.bm25_needed = true;
        }
        let left = res["pending"].as_u64().unwrap_or(0) as usize;
        let was = self.vec_pending.unwrap_or(0);
        if left > 0 && (self.vec_pending.is_none() || left > self.vec_total) {
            self.vec_total = left;
            crate::info!("autoindex", "벡터 대기 청크 {}개 — 질문이 없을 때 채운다", left);
        }
        if left == 0 && was > 0 {
            crate::info!("autoindex", "벡터 채우기 끝 — {}청크", self.vec_total);
            if self.vec_total >= VEC_SHOW_MIN {
                (self.notify)("info", "벡터 채우기가 끝났습니다 — 이제 일상어 질문도 원래 정확도로 찾습니다");
            }
            self.vec_total = 0;
        }
        self.vec_pending = Some(left);
        // 탐색기 창 맨 아래 줄의 진행도 새로 고친다
        self.prog_ver += 1;
    }

    fn on_bm25(&mut self, res: &Value) {
        if Self::b(res, "stopped") {
            return;
        }
        self.bm25_needed = false;
        self.bm25_mid_docs = 0;
        self.bm25_mid_at = Instant::now();
        if let Some(n) = res.get("pending").and_then(|v| v.as_u64()) {
            if n > 0 && self.vec_pending.unwrap_or(0) == 0 {
                self.vec_pending = None;
            }
        }
        if Self::b(res, "ok") {
            crate::info!("autoindex", "키워드 색인 다시 만듦: {}청크 {}ms", res["chunks"], res["ms"]);
        } else {
            crate::warn!("autoindex", "키워드 색인 실패: {}", res["why"]);
        }
    }

    //------------------------------------------------------------------
    // 키워드 색인 중간 반영을 할 때인가
    //=> 반영할 문서가 있고 대기열이 남았으며, 문서 수 · 시간 문턱에 닿았을 때 참.
    //   지난 반영이 오래 걸렸으면 그 배수만큼 쉬어 처리 시간의 약 5% 안으로 묶는다.
    //
    // -in: 없음
    //
    // -out: true = 지금 /bm25 {"merge": false} 를 보낸다
    // -out: error = 없음
    //------------------------------------------------------------------
    fn bm25_mid_due(&self) -> bool {
        if !self.bm25_needed || self.bm25_mid_docs == 0 || self.docs.is_empty() {
            return false;
        }
        let gap = self.bm25_mid_at.elapsed();
        let due = self.bm25_mid_docs >= BM25_MID_DOCS || gap >= Duration::from_secs(BM25_MID_S);
        due && gap >= self.bm25_mid_cost * BM25_MID_REST
    }

    //------------------------------------------------------------------
    // 키워드 색인 중간 반영 결과
    //=> 라운드 끝의 합치기 · 메타 마무리는 남아 있으므로 bm25_needed 는 그대로 둔다.
    //
    // -in: res = 워커 결과 {ok, chunks, mode, pending, ms}
    //
    // -out: 없음
    // -out: error = 없음
    //------------------------------------------------------------------
    fn on_bm25_mid(&mut self, res: &Value) {
        if Self::b(res, "stopped") {
            return;
        }
        self.bm25_mid_docs = 0;
        self.bm25_mid_at = Instant::now();
        self.bm25_mid_cost = Duration::from_millis(res["ms"].as_u64().unwrap_or(0));
        if Self::b(res, "ok") {
            crate::info!("autoindex", "키워드 색인 중간 반영: {}청크 {}ms ({})", res["chunks"], res["ms"], res["mode"]);
        } else {
            crate::warn!("autoindex", "키워드 색인 중간 반영 실패: {}", res["why"]);
        }
    }

    //------------------------------------------------------------------
    // 워커가 내려가 있을 때 — index 명령을 따로 돌린다
    //------------------------------------------------------------------
    fn run_cli(&mut self) {
        if self.base_cmd.is_empty() {
            return;
        }
        let mut roots: Vec<String> = self.dirty.iter().map(|(r, _)| r.clone()).collect();
        for (_, _, r) in &self.docs {
            if let Some(r) = r {
                if !roots.contains(r) {
                    roots.push(r.clone());
                }
            }
        }
        self.dirty.clear();
        self.docs.clear();
        self.queued.clear();
        let mut jobs = vec![];
        for r in &roots {
            let mut args = vec!["index".to_string(), "--dir".into(), r.clone()];
            if self.approved.contains(r) {
                args.push("--allow-delete".into());
            } else if self.s.autoindex_ask_above > 0 {
                args.push("--max-add".into());
                args.push(self.s.autoindex_ask_above.to_string());
            }
            jobs.push((r.clone(), args));
            self.approved.remove(r);
        }
        if let Some(w) = &self.worker {
            w.hold(Some("index 명령 실행 중".into()));
            if w.availability() != "down" {
                w.hold(None);
                for (r, _) in &jobs {
                    self.mark_dirty(r, "워커가 떠서 워커에게 맡김");
                }
                return;
            }
        }
        self.inflight = Some("index 명령".into());
        crate::info!("autoindex", "워커가 내려가 있어 index 명령으로 반영한다: {}", roots.join(", "));
        let base = self.base_cmd.clone();
        let post = self.post.clone();
        let exts = self.s.index_exts.join(" ");
        let exclude = self.s.exclude_folders.join(";");
        let job = self.worker.as_ref().and_then(|w| w.job_handle());
        std::thread::spawn(move || {
            let mut results = vec![];
            for (root, args) in jobs {
                let t0 = Instant::now();
                let mut c = std::process::Command::new(&base[0]);
                c.args(&base[1..]).args(&args).stdin(std::process::Stdio::null());
                // 자동 인덱싱은 사용자가 다른 일을 하는 중에 돈다 — 임베딩 스레드를 4 로 둔다(설계서 §3-2)
                c.env("SIMPLERAG_EMBED_THREADS", "4");
                c.env("SIMPLERAG_INDEX_EXTS", &exts);
                c.env("SIMPLERAG_INDEX_EXCLUDE", &exclude);
                if let Some(d) = std::path::Path::new(&base[0]).parent() {
                    c.current_dir(d);
                }
                {
                    use std::os::windows::process::CommandExt;
                    c.creation_flags(0x0800_0000);
                }
                let (mut summary, mut code) = (None, None);
                let tail;
                c.stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped());
                match c.spawn() {
                    Ok(mut ch) => {
                        // 앱을 꺼도 혼자 남아 돌면 인덱스를 쥔 채 프로그램 제거 · 다음 실행을 막는다 — 앱과 함께 끝낸다.
                        // 끊긴 문서는 다음 대조에서 다시 넣는다
                        if let Some(j) = job {
                            crate::win::assign_job(windows::Win32::Foundation::HANDLE(j as *mut _), &ch);
                        }
                        // stderr 는 따로 받아 둔다 — 안 읽으면 버퍼가 차서 index 명령이 멈출 수 있다
                        let err = ch.stderr.take();
                        let eh = std::thread::spawn(move || {
                            let mut b = vec![];
                            if let Some(mut e) = err {
                                let _ = std::io::Read::read_to_end(&mut e, &mut b);
                            }
                            b
                        });
                        // stdout 은 한 줄씩 읽어 "[10/311]" 진행을 탐색 창으로 넘긴다
                        let mut raw: Vec<u8> = vec![];
                        if let Some(so) = ch.stdout.take() {
                            let mut r = std::io::BufReader::new(so);
                            let mut line = vec![];
                            loop {
                                line.clear();
                                match std::io::BufRead::read_until(&mut r, b'\n', &mut line) {
                                    Ok(n) if n > 0 => {}
                                    _ => break,
                                }
                                if let Some((n, total)) = parse_progress(&String::from_utf8_lossy(&line)) {
                                    post(JobMsg::CliProgress(root.clone(), n, total));
                                }
                                raw.extend_from_slice(&line);
                            }
                        }
                        let eb = eh.join().unwrap_or_default();
                        code = ch.wait().ok().and_then(|s| s.code());
                        let out = String::from_utf8_lossy(&raw).into_owned() + &String::from_utf8_lossy(&eb);
                        for ln in out.lines() {
                            if let Some(js) = ln.strip_prefix(SUMMARY_PREFIX) {
                                summary = serde_json::from_str::<Value>(js).ok();
                            } else if !ln.trim().is_empty() {
                                crate::log::worker(&format!("[index] {}", ln.trim_end()));
                            }
                        }
                        let lines: Vec<&str> = out.trim().lines().collect();
                        tail = lines[lines.len().saturating_sub(3)..].join("\n");
                    }
                    Err(e) => tail = e.to_string(),
                }
                results.push((root, code, summary, tail, t0.elapsed().as_secs_f64()));
            }
            post(JobMsg::Cli(results));
        });
    }

    fn on_cli(&mut self, results: Vec<(String, Option<i32>, Option<Value>, String, f64)>) {
        self.inflight = None;
        if let Some(w) = &self.worker {
            w.hold(None);
        }
        let mut failed_any = false;
        for (root, code, summary, tail, sec) in results {
            self.prog.remove(&root);
            self.prog_ver += 1;
            let ok = summary.as_ref().map(|s| Self::b(s, "ok")).unwrap_or(false);
            if !ok {
                failed_any = true;
                let why = summary.as_ref().and_then(|s| s["why"].as_str().map(|x| x.to_string())).unwrap_or(tail);
                crate::warn!("autoindex", "index 명령 실패(code={:?}, {:.1}s): {} — {}", code, sec, root, why);
                self.mark_dirty(&root, "index 명령 실패 뒤 다시");
                continue;
            }
            let s = summary.unwrap();
            let n = |k: &str| s[k].as_u64().unwrap_or(0) as usize;
            let changed = n("added") + n("modified") + n("removed");
            crate::info!("autoindex", "index 명령 {}: 추가 {} · 수정 {} · 삭제 {} · 실패 {} ({:.1}s)", root, n("added"), n("modified"), n("removed"), n("failed"), sec);
            self.round.done += changed;
            self.round.total += changed;
            self.round.changed.extend(std::iter::repeat("?".to_string()).take(changed));
            self.round.failed.extend(std::iter::repeat("?".to_string()).take(n("failed")));
            let held = n("held_new");
            if held > 0 {
                if self.waiting_new.get(&root) != Some(&held) {
                    (self.notify)("ask", &format!("새 문서 {}건이 있습니다", held));
                }
                self.waiting_new.insert(root.clone(), held);
            } else {
                self.waiting_new.remove(&root);
            }
            let db = s["delete_blocked"].as_str().unwrap_or("").to_string();
            if !db.is_empty() {
                if !self.waiting_delete.contains_key(&root) {
                    (self.notify)("ask", &format!("{}\n정말 지운 문서라면 인덱스에서도 뺍니다.", db));
                }
                self.waiting_delete.insert(root, db);
            } else {
                self.waiting_delete.remove(&root);
            }
        }
        if failed_any {
            self.retry_at = Some(Instant::now() + Duration::from_secs(CLI_RETRY_S));
        }
    }

    fn round_active(&self) -> bool {
        self.round.total > 0 || !self.round.failed.is_empty()
    }

    // 한 차례가 끝나면 3건 이상일 때만 트레이로 알린다
    fn finish_round(&mut self) {
        let (n, failed) = (self.round.changed.len(), self.round.failed.len());
        if n >= NOTIFY_AT_LEAST || failed > 0 {
            let msg = if n > 0 {
                let mut m = format!("문서 {}건을 인덱스에 반영했습니다", n);
                if failed > 0 {
                    m += &format!(" · 실패 {}건(로그 참고 — 옛 버전은 그대로 남아 있습니다)", failed);
                }
                m
            } else {
                format!("문서 {}건을 인덱스에 반영하지 못했습니다(로그 참고 — 옛 버전은 그대로 남아 있습니다)", failed)
            };
            (self.notify)("info", &msg);
        }
        if n > 0 || failed > 0 {
            crate::info!("autoindex", "자동 인덱싱 한 차례 끝: 반영 {} · 실패 {}", n, failed);
        }
        self.round = Round { done: 0, total: 0, changed: vec![], failed: vec![] };
        self.prog.clear();
        self.running_approved.clear();
        self.prog_ver += 1;
    }
}

//------------------------------------------------------------------
// index 명령 출력 한 줄에서 진행 읽기 — "  [10/311] [추가] …" → (10, 311), 아니면 None
//------------------------------------------------------------------
fn parse_progress(line: &str) -> Option<(usize, usize)> {
    let rest = line.trim_start().strip_prefix('[')?;
    let (head, _) = rest.split_once(']')?;
    let (n, total) = head.split_once('/')?;
    Some((n.parse().ok()?, total.parse().ok()?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn progress_line() {
        assert_eq!(parse_progress("  [10/311] [추가] 규정.docx   5청크\n"), Some((10, 311)));
        assert_eq!(parse_progress("  [삭제] 규정.docx"), None);
        assert_eq!(parse_progress("문서 3건 중 3건 처리 대상"), None);
    }
}
