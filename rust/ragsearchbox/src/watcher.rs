//------------------------------------------------------------------
// 폴더 감시 (Python watcher.py)
//=> 지정 폴더마다 ReadDirectoryChangesW 로 바뀐 문서를 알아채고, 조용해진 뒤(quiet_s) 다 써진
//   파일만 묶음(Batch)으로 넘긴다. 하위 폴더 변화·알림 넘침·순찰 주기·폴더 재연결 때는 '전체 대조'를 부탁한다.
//   폴더가 안 보이면(드라이브 분리) 30초마다 다시 열어 본다.
//------------------------------------------------------------------

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use windows::core::HSTRING;
use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows::Win32::Storage::FileSystem::*;
use windows::Win32::System::Threading::{CreateEventW, ResetEvent, SetEvent, WaitForMultipleObjects};
use windows::Win32::System::IO::{CancelIo, GetOverlappedResult, OVERLAPPED};

const TEXT_EXTS: &[&str] = &[".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".hwp", ".hwpx", ".txt", ".md", ".html", ".htm", ".py", ".ipynb"];
const BUF_SIZE: usize = 64 * 1024;
const OPEN_RETRY_S: [u64; 3] = [5, 15, 60];
const OFFLINE_RETRY_S: u32 = 30;

//------------------------------------------------------------------
// 문서 이름인가(임시 파일 제외, 확장자)
//------------------------------------------------------------------
pub fn is_doc_name(path: &str) -> bool {
    let name = path.rsplit(['\\', '/']).next().unwrap_or("");
    if name.starts_with("~$") || name.starts_with(".~lock.") {
        return false;
    }
    let ext = name.rfind('.').map(|i| name[i..].to_lowercase()).unwrap_or_default();
    TEXT_EXTS.contains(&ext.as_str())
}

fn is_hidden(path: &str) -> bool {
    use std::os::windows::fs::MetadataExt;
    std::fs::metadata(path).map(|m| m.file_attributes() & 0x6 != 0).unwrap_or(false)
}
fn file_stat(path: &str) -> Option<(u64, u128)> {
    let m = std::fs::metadata(path).ok()?;
    let t = m.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?.as_nanos();
    Some((m.len(), t))
}
fn can_open(path: &str) -> bool {
    use std::io::Read;
    std::fs::File::open(path).and_then(|mut f| f.read(&mut [0u8; 1])).is_ok()
}

// 판정 대기 중인 파일
struct Pending {
    path: String,
    last: Instant,
    stat: Option<(u64, u128)>,
    tries: usize,
    next_at: Instant,
}

//------------------------------------------------------------------
// 다 써졌나 — wait | changed | removed | drop
//=> 조용해진 뒤 크기·시각을 두 번 재 같으면, 읽기로 열리는지까지 본다.
//------------------------------------------------------------------
fn settle(p: &mut Pending, now: Instant, quiet: Duration, stable: Duration) -> &'static str {
    if now < p.next_at || now - p.last < quiet {
        return "wait";
    }
    let st = match file_stat(&p.path) {
        Some(s) => s,
        None => return "removed",
    };
    if Some(st) != p.stat {
        p.stat = Some(st);
        p.next_at = now + stable;
        return "wait";
    }
    if can_open(&p.path) {
        return "changed";
    }
    if p.tries >= OPEN_RETRY_S.len() {
        return "drop";
    }
    p.next_at = now + Duration::from_secs(OPEN_RETRY_S[p.tries]);
    p.tries += 1;
    p.stat = None;
    "wait"
}

//------------------------------------------------------------------
// 묶음 — 그 폴더에서 바뀐 것·지운 것·전체 대조 여부
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Batch {
    pub root: String,
    pub changed: Vec<String>,
    pub removed: Vec<String>,
    pub rescan: bool,
    pub reason: String,
}

struct RootState {
    pending: HashMap<String, Pending>,
    rescan_at: Option<Instant>,
    rescan_why: String,
    last_rescan: Option<Instant>,
    online: Option<bool>,
}

//------------------------------------------------------------------
// 폴더 감시자
//------------------------------------------------------------------
pub struct FolderWatcher {
    pub roots: Vec<String>,
    on_batch: Arc<dyn Fn(Batch) + Send + Sync>,
    quiet: Duration,
    rescan: Duration,
    st: Mutex<HashMap<String, RootState>>,
    stop: AtomicBool,
    stop_ev: Mutex<Vec<isize>>,
}

impl FolderWatcher {
    pub fn new(roots: &[String], on_batch: Arc<dyn Fn(Batch) + Send + Sync>, quiet_s: i64, rescan_min: i64) -> Arc<FolderWatcher> {
        let roots: Vec<String> = roots.iter().map(|r| std::path::absolute(r).map(|p| p.to_string_lossy().into_owned()).unwrap_or(r.clone())).collect();
        let st = roots.iter().map(|r| (r.clone(), RootState { pending: HashMap::new(), rescan_at: None, rescan_why: String::new(), last_rescan: None, online: None })).collect();
        Arc::new(FolderWatcher { roots, on_batch, quiet: Duration::from_secs(quiet_s.max(1) as u64), rescan: Duration::from_secs(rescan_min.max(0) as u64 * 60),
                                 st: Mutex::new(st), stop: AtomicBool::new(false), stop_ev: Mutex::new(vec![]) })
    }

    //------------------------------------------------------------------
    // 시작 — 폴더마다 알림 스레드 + 판정 스레드(0.5초)
    //------------------------------------------------------------------
    pub fn start(self: &Arc<Self>) {
        for r in &self.roots {
            self.request_rescan(r, "시작", true);
        }
        for r in self.roots.clone() {
            let ev = unsafe { CreateEventW(None, true, false, None) }.map(|h| h.0 as isize).unwrap_or(0);
            self.stop_ev.lock().unwrap().push(ev);
            let me = self.clone();
            std::thread::spawn(move || me.watch_root(&r, ev));
        }
        let me = self.clone();
        std::thread::spawn(move || {
            while !me.stop.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(500));
                me.tick();
            }
        });
        crate::info!("watcher", "폴더 감시 시작: {} (조용히 {}초 · 순찰 {})", self.roots.join(", "), self.quiet.as_secs(),
                     if self.rescan.is_zero() { "시작 때만".into() } else { format!("{}분", self.rescan.as_secs() / 60) });
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        for ev in self.stop_ev.lock().unwrap().iter() {
            unsafe {
                let _ = SetEvent(HANDLE(*ev as *mut _));
            }
        }
    }

    //------------------------------------------------------------------
    // 전체 대조 부탁 — immediate 면 조용히 기다리지 않는다
    //------------------------------------------------------------------
    pub fn request_rescan(&self, root: &str, reason: &str, immediate: bool) {
        let now = Instant::now();
        let mut g = self.st.lock().unwrap();
        if let Some(s) = g.get_mut(root) {
            s.rescan_at = Some(if immediate { now.checked_sub(self.quiet).unwrap_or(now) } else { now });
            if !s.rescan_why.contains(reason) {
                s.rescan_why = format!("{}, {}", s.rescan_why, reason).trim_matches([',', ' ']).to_string();
            }
        }
    }

    // 파일 알림 하나 적기
    fn note(&self, root: &str, path: &str) {
        if !is_doc_name(path) {
            return;
        }
        let now = Instant::now();
        let key = path.to_lowercase();
        let mut g = self.st.lock().unwrap();
        if let Some(s) = g.get_mut(root) {
            match s.pending.get_mut(&key) {
                Some(p) => {
                    p.last = now;
                    p.stat = None;
                    p.tries = 0;
                    p.next_at = now;
                }
                None => {
                    s.pending.insert(key, Pending { path: path.to_string(), last: now, stat: None, tries: 0, next_at: now });
                }
            }
        }
    }

    //------------------------------------------------------------------
    // 판정 — 다 써진 파일·지운 파일·전체 대조를 묶어 넘긴다
    //------------------------------------------------------------------
    fn tick(&self) {
        let now = Instant::now();
        for root in &self.roots {
            let mut changed = vec![];
            let mut removed = vec![];
            let (rescan, why) = {
                let mut g = self.st.lock().unwrap();
                let s = g.get_mut(root).unwrap();
                let mut done = vec![];
                for (k, p) in s.pending.iter_mut() {
                    let v = settle(p, now, self.quiet, Duration::from_secs(1));
                    match v {
                        "wait" => continue,
                        "changed" => {
                            if !is_hidden(&p.path) {
                                changed.push(p.path.clone());
                            }
                        }
                        "removed" => removed.push(p.path.clone()),
                        _ => crate::warn!("watcher", "계속 열 수 없어 다음 순찰로 미룬다: {}", p.path),
                    }
                    done.push(k.clone());
                }
                for k in done {
                    s.pending.remove(&k);
                }
                let mut rescan = false;
                let mut why = String::new();
                if let Some(a) = s.rescan_at {
                    if now.saturating_duration_since(a) >= self.quiet {
                        rescan = true;
                        why = s.rescan_why.clone();
                    }
                }
                if !rescan && !self.rescan.is_zero() && s.last_rescan.map(|t| now - t >= self.rescan).unwrap_or(false) {
                    rescan = true;
                    why = "순찰".into();
                }
                if rescan {
                    s.rescan_at = None;
                    s.rescan_why.clear();
                    s.last_rescan = Some(now);
                } else if s.last_rescan.is_none() {
                    s.last_rescan = Some(now);
                }
                (rescan, why)
            };
            if !changed.is_empty() || !removed.is_empty() || rescan {
                let b = Batch { root: root.clone(), changed, removed, rescan, reason: why };
                crate::diag!("watcher", "감시 묶음: {:?}", b);
                (self.on_batch)(b);
            }
        }
    }

    fn set_online(&self, root: &str, online: bool, err: &str) {
        let prev = {
            let mut g = self.st.lock().unwrap();
            let s = g.get_mut(root).unwrap();
            let p = s.online;
            s.online = Some(online);
            p
        };
        if prev == Some(online) {
            return;
        }
        if online {
            if prev == Some(false) {
                crate::info!("watcher", "폴더가 다시 보인다 — 전체 대조: {}", root);
                self.request_rescan(root, "다시 연결됨", true);
            }
        } else {
            crate::warn!("watcher", "폴더를 지켜볼 수 없다({}초 뒤 다시): {} — {}", OFFLINE_RETRY_S, root, err);
        }
    }

    //------------------------------------------------------------------
    // 폴더 하나 지켜보기 — 파일 이름·크기·쓰기 / 하위 폴더 이름 두 갈래
    //------------------------------------------------------------------
    fn watch_root(self: Arc<Self>, root: &str, stop_ev: isize) {
        let file_flags = FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE;
        let dir_flags = FILE_NOTIFY_CHANGE_DIR_NAME;
        while !self.stop.load(Ordering::SeqCst) {
            let mut watches: Vec<(HANDLE, Box<OVERLAPPED>, Vec<u8>, FILE_NOTIFY_CHANGE)> = vec![];
            let mut fail = None;
            for flags in [file_flags, dir_flags] {
                let hdir = unsafe {
                    CreateFileW(&HSTRING::from(root), FILE_LIST_DIRECTORY.0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, None,
                                OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED, None)
                };
                match hdir {
                    Ok(hd) => {
                        let ev = unsafe { CreateEventW(None, true, false, None) }.unwrap_or_default();
                        let ov = Box::new(OVERLAPPED { hEvent: ev, ..Default::default() });
                        watches.push((hd, ov, vec![0u8; BUF_SIZE], flags));
                    }
                    Err(e) => {
                        fail = Some(e.to_string());
                        break;
                    }
                }
            }
            if let Some(e) = fail {
                close(&mut watches);
                self.set_online(root, false, &e);
                let r = unsafe { WaitForMultipleObjects(&[HANDLE(stop_ev as *mut _)], false, OFFLINE_RETRY_S * 1000) };
                if r == WAIT_OBJECT_0 {
                    return;
                }
                continue;
            }
            self.set_online(root, true, "");
            // 감시 걸기
            let arm = |w: &mut (HANDLE, Box<OVERLAPPED>, Vec<u8>, FILE_NOTIFY_CHANGE)| unsafe {
                ReadDirectoryChangesW(w.0, w.2.as_mut_ptr() as *mut _, BUF_SIZE as u32, true, w.3, None, Some(&mut *w.1 as *mut _), None).is_ok()
            };
            let mut ok = watches.iter_mut().all(arm);
            while ok && !self.stop.load(Ordering::SeqCst) {
                let handles = [watches[0].1.hEvent, watches[1].1.hEvent, HANDLE(stop_ev as *mut _)];
                let rc = unsafe { WaitForMultipleObjects(&handles, false, 1000) };
                if rc == WAIT_TIMEOUT {
                    if !std::path::Path::new(root).is_dir() {
                        self.set_online(root, false, "폴더가 그 자리에 없다(이름 변경·이동·분리)");
                        ok = false;
                    }
                    continue;
                }
                let idx = (rc.0 - WAIT_OBJECT_0.0) as usize;
                if idx == 2 {
                    close(&mut watches);
                    return;
                }
                if idx > 2 {
                    ok = false;
                    continue;
                }
                let w = &mut watches[idx];
                let mut n = 0u32;
                let got = unsafe { GetOverlappedResult(w.0, &*w.1, &mut n, false) }.is_ok();
                unsafe {
                    let _ = ResetEvent(w.1.hEvent);
                }
                if !got {
                    ok = false;
                    continue;
                }
                if n == 0 {
                    self.request_rescan(root, "알림 넘침", false);
                } else if idx == 1 {
                    self.request_rescan(root, "하위 폴더 변화", false);
                } else {
                    for name in parse_notify(&w.2[..n as usize]) {
                        let p = format!("{}\\{}", root.trim_end_matches('\\'), name);
                        self.note(root, &p);
                    }
                }
                ok = arm(w);
            }
            close(&mut watches);
            if !self.stop.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_secs(1));
            }
        }
    }
}

// FILE_NOTIFY_INFORMATION 목록 → 상대 경로들
fn parse_notify(buf: &[u8]) -> Vec<String> {
    let mut out = vec![];
    let mut off = 0usize;
    loop {
        if off + 12 > buf.len() {
            break;
        }
        let next = u32::from_le_bytes(buf[off..off + 4].try_into().unwrap()) as usize;
        let len = u32::from_le_bytes(buf[off + 8..off + 12].try_into().unwrap()) as usize;
        let s = off + 12;
        if s + len > buf.len() {
            break;
        }
        let u: Vec<u16> = buf[s..s + len].chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        out.push(String::from_utf16_lossy(&u));
        if next == 0 {
            break;
        }
        off += next;
    }
    out
}

fn close(watches: &mut Vec<(HANDLE, Box<OVERLAPPED>, Vec<u8>, FILE_NOTIFY_CHANGE)>) {
    for w in watches.drain(..) {
        unsafe {
            let _ = CancelIo(w.0);
            let _ = CloseHandle(w.0);
            let _ = CloseHandle(w.1.hEvent);
        }
    }
}
