//------------------------------------------------------------------
// 자동 인덱싱 (Python index_jobs.py)
//=> 폴더 감시 묶음을 받아 "대조 → 문서별 반영 → 키워드 색인" 을 워커에게 하나씩 시킨다.
//    · 워커가 떠 있으면: /index-outside → /index-plan → /index-doc·/remove-doc → /bm25
//    · 워커가 내려가 있으면: `simplerag-rs index --dir` 를 따로 돌린다(그동안 워커를 띄우지 않는다)
//    · 새 문서가 너무 많거나(AskAboveDocs) 한꺼번에 많이 사라지면 멈추고 트레이 '지금 인덱싱' 허락을 기다린다
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
const OUTSIDE_KEY: &str = "(지정 폴더 밖)";

//------------------------------------------------------------------
// 메인 스레드로 돌아오는 결과
//------------------------------------------------------------------
pub enum JobMsg {
    Done(u64, Value),
    Cli(Vec<(String, Option<i32>, Option<Value>, String, f64)>),
}

// 보낸 명령의 결과를 받을 곳
enum Handler {
    Outside,
    Plan(String),
    Doc(String, String, Option<String>),
    Bm25,
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
            queued: HashSet::new(), inflight: None, handlers: HashMap::new(), next_id: 1, bm25_needed: false, retry_at: None,
            last_avail: None, outside_needed: true, approve_outside: false, waiting_new: HashMap::new(), waiting_delete: HashMap::new(),
            round: Round { done: 0, total: 0, changed: vec![], failed: vec![] },
            vec_pending: None, vec_total: 0,
        }
    }

    // 벡터 채우기 진행 글 — 대기 청크가 없으면 None
    fn pending_text(&self) -> Option<String> {
        let left = self.vec_pending.filter(|n| *n > 0)?;
        let total = self.vec_total.max(left);
        Some(format!("벡터 채우는 중 {} / {} — 지금도 검색할 수 있습니다", comma(total - left), comma(total)))
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

    pub fn rescan_all(&mut self) {
        for r in self.roots.clone() {
            self.mark_dirty(&r, "사용자 요청");
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
                return format!("인덱스 갱신 중 {}/{}", self.round.done, self.round.total);
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
                return format!("인덱싱 중 {}/{}", self.round.done, self.round.total);
            }
            return "인덱스 확인 중".into();
        }
        if !self.waiting_new.is_empty() || !self.waiting_delete.is_empty() {
            return "'지금 인덱싱' 필요".into();
        }
        match self.vec_pending {
            Some(n) if n > 0 => format!("벡터 채우는 중 {}/{}", comma(self.vec_total.max(n) - n), comma(self.vec_total.max(n))),
            _ => String::new(),
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
        if let Some((op, path, root)) = self.docs.pop_front() {
            self.queued.remove(&(op.clone(), path.to_lowercase()));
            let line = if op == "remove-outside" {
                format!("/remove-doc {}", crate::worker::ascii_json(&json!({"path": path, "outside_of": self.roots})))
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
        if idle && self.vec_pending.map(|n| n > 0).unwrap_or(true) {
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
                    Handler::Pending => self.on_pending(&res),
                }
            }
            JobMsg::Cli(results) => self.on_cli(results),
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
        let mut add = Self::strs(res, "add");
        let modify = Self::strs(res, "modify");
        let remove = Self::strs(res, "remove");
        let mv = Self::strs(res, "move");
        let limit = self.s.autoindex_ask_above as usize;
        if limit > 0 && add.len() > limit && !approved {
            if self.waiting_new.get(root) != Some(&add.len()) {
                (self.notify)("warn", &format!("새 문서 {}건이 있습니다 — 트레이 메뉴 '지금 인덱싱' 을 누르면 넣습니다", add.len()));
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
                (self.notify)("warn", &format!("{} — 정말 지운 것이면 트레이 메뉴 '지금 인덱싱'", db));
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
                (self.notify)("warn", &format!("지정 폴더 밖 문서 {}건이 인덱스에 있습니다({}) — 트레이 '지금 인덱싱' 을 누르면 뺍니다", out.len(), db));
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
                Some(r) => self.mark_dirty(&r, "워커가 도중에 내려감"),
            }
            return;
        }
        self.round.done += 1;
        let result = res["result"].as_str().unwrap_or("");
        let name = basename(path);
        if Self::b(res, "ok") && result == "moved" {
            self.round.changed.push(name.clone());
            crate::info!("autoindex", "반영 이동: {} → {}", res["from"].as_str().unwrap_or("?"), path);
            (self.notify)("doc", &format!("방금 반영(이동): {}", name));
        } else if Self::b(res, "ok") && ["added", "modified", "copied", "removed"].contains(&result) {
            self.bm25_needed = true;
            self.round.changed.push(name.clone());
            let label = match result { "added" => "추가", "modified" => "수정", "copied" => "복사", _ => "삭제" };
            crate::info!("autoindex", "반영 {}: {} ({}청크, {}ms)", label, path, res.get("chunks").or(res.get("removed")).cloned().unwrap_or(json!("")), res["ms"]);
            (self.notify)("doc", &format!("방금 반영({}): {}", label, name));
        } else if !Self::b(res, "ok") {
            self.round.failed.push(name);
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
        let left = res["pending"].as_u64().unwrap_or(0) as usize;
        let was = self.vec_pending.unwrap_or(0);
        if left > 0 && (self.vec_pending.is_none() || left > self.vec_total) {
            self.vec_total = left;
            crate::info!("autoindex", "벡터 대기 청크 {}개 — 질문이 없을 때 채운다", left);
        }
        if left == 0 && was > 0 {
            crate::info!("autoindex", "벡터 채우기 끝 — {}청크", self.vec_total);
            (self.notify)("info", "벡터 채우기가 끝났습니다 — 이제 일상어 질문도 원래 정확도로 찾습니다");
            self.vec_total = 0;
        }
        self.vec_pending = Some(left);
    }

    fn on_bm25(&mut self, res: &Value) {
        if Self::b(res, "stopped") {
            return;
        }
        self.bm25_needed = false;
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
        std::thread::spawn(move || {
            let mut results = vec![];
            for (root, args) in jobs {
                let t0 = Instant::now();
                let mut c = std::process::Command::new(&base[0]);
                c.args(&base[1..]).args(&args).stdin(std::process::Stdio::null());
                // 자동 인덱싱은 사용자가 다른 일을 하는 중에 돈다 — 임베딩 스레드를 4 로 둔다(설계서 §3-2)
                c.env("SIMPLERAG_EMBED_THREADS", "4");
                if let Some(d) = std::path::Path::new(&base[0]).parent() {
                    c.current_dir(d);
                }
                {
                    use std::os::windows::process::CommandExt;
                    c.creation_flags(0x0800_0000);
                }
                let (mut summary, mut code) = (None, None);
                let tail;
                match c.output() {
                    Ok(o) => {
                        code = o.status.code();
                        let out = String::from_utf8_lossy(&o.stdout).into_owned() + &String::from_utf8_lossy(&o.stderr);
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
                    (self.notify)("warn", &format!("새 문서 {}건이 있습니다 — 트레이 메뉴 '지금 인덱싱' 을 누르면 넣습니다", held));
                }
                self.waiting_new.insert(root.clone(), held);
            } else {
                self.waiting_new.remove(&root);
            }
            let db = s["delete_blocked"].as_str().unwrap_or("").to_string();
            if !db.is_empty() {
                if !self.waiting_delete.contains_key(&root) {
                    (self.notify)("warn", &format!("{} — 정말 지운 것이면 트레이 메뉴 '지금 인덱싱'", db));
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
    }
}
