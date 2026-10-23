//------------------------------------------------------------------
// 탐색기 감시 (Python monitor.py 의 폴더 패널 부분)
//=> 자기 스레드(COM 단일 스레드 아파트)에서 FolderPollMs 마다
//    1) 전경 탐색기 창과 패널이 붙은 창이 보는 폴더를 알아낸다
//    2) 범위 안이면 그 폴더를, 밖이면 None 을 바뀔 때만 알린다(on_folder)
//    3) 패널이 붙은 창과 다른 탐색기가 0.8초 넘게 앞에 있으면 패널을 그쪽으로 옮기라고 알린다
//   "탐색기에서 보기" 요청도 이 스레드가 UIA 로 처리한다(COM 객체를 한 스레드에 모은다).
//------------------------------------------------------------------

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use windows::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};

use crate::explorer::{self, Locator};
use crate::scope::Scope;
use crate::win;

const SWITCH_DWELL: Duration = Duration::from_millis(800);

//------------------------------------------------------------------
// 감시가 앱에 알리는 일
//------------------------------------------------------------------
pub enum MEvent {
    Folder(isize, Option<String>),
    Note(String, String),
}

//------------------------------------------------------------------
// 감시 손잡이 — 메인 스레드가 쓴다
//------------------------------------------------------------------
pub struct Monitor {
    pub watch_hwnd: Arc<AtomicIsize>,
    pub folder_seen: Arc<Mutex<HashMap<isize, Option<String>>>>,
    req: Sender<(isize, Vec<String>)>,
    stop: Arc<AtomicBool>,
}

impl Monitor {
    //------------------------------------------------------------------
    // 시작
    // -in: scope = 범위(메인과 함께 씀) / poll_ms = 폴더 확인 주기 / emit = 알림 함수
    //------------------------------------------------------------------
    pub fn start(scope: Arc<Mutex<Scope>>, poll_ms: i64, panel_enabled: bool, emit: Arc<dyn Fn(MEvent) + Send + Sync>) -> Monitor {
        let (tx, rx) = std::sync::mpsc::channel::<(isize, Vec<String>)>();
        let m = Monitor { watch_hwnd: Arc::new(AtomicIsize::new(0)), folder_seen: Arc::new(Mutex::new(HashMap::new())), req: tx, stop: Arc::new(AtomicBool::new(false)) };
        let (w, fs, st) = (m.watch_hwnd.clone(), m.folder_seen.clone(), m.stop.clone());
        std::thread::Builder::new().name("rsb-monitor".into()).spawn(move || {
            unsafe {
                let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
            }
            run(scope, poll_ms.max(100) as u64, panel_enabled, emit, w, fs, st, rx);
        }).ok();
        m
    }

    pub fn watch_window(&self, hwnd: Option<isize>) {
        self.watch_hwnd.store(hwnd.unwrap_or(0), Ordering::SeqCst);
    }

    // 근거 파일을 탐색기 검색창에 띄워 달라
    pub fn show_evidence(&self, hwnd: isize, names: Vec<String>) {
        let _ = self.req.send((hwnd, names));
    }

    //------------------------------------------------------------------
    // 범위 폴더를 보고 있는 보이는 탐색기 창들(z 순서)
    //------------------------------------------------------------------
    pub fn in_scope_windows(&self) -> Vec<(isize, String)> {
        let seen = self.folder_seen.lock().unwrap().clone();
        win::enum_windows().into_iter().filter_map(|h| match seen.get(&h) {
            Some(Some(f)) if win::is_visible(h) => Some((h, f.clone())),
            _ => None,
        }).collect()
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

#[allow(clippy::too_many_arguments)]
fn run(scope: Arc<Mutex<Scope>>, poll_ms: u64, panel_enabled: bool, emit: Arc<dyn Fn(MEvent) + Send + Sync>, watch: Arc<AtomicIsize>,
       seen: Arc<Mutex<HashMap<isize, Option<String>>>>, stop: Arc<AtomicBool>, rx: Receiver<(isize, Vec<String>)>) {
    let mut loc = Locator::new();
    let mut fg_candidate: Option<isize> = None;
    let mut fg_since = Instant::now();
    let mut last_forget = Instant::now();
    while !stop.load(Ordering::SeqCst) {
        // 근거 목록 요청
        while let Ok((hwnd, names)) = rx.try_recv() {
            match explorer::show_evidence(hwnd, &names) {
                Ok(q) => emit(MEvent::Note("evidence".into(), q)),
                Err(e) => emit(MEvent::Note("evidence_fail".into(), e)),
            }
        }
        if panel_enabled {
            let mut targets = vec![];
            let fg = win::foreground_explorer();
            if let Some(f) = fg {
                targets.push(f);
            }
            let w = watch.load(Ordering::SeqCst);
            if w != 0 && !targets.contains(&w) {
                targets.push(w);
            }
            for hwnd in targets {
                let (paths, why) = loc.folder_of(hwnd, "");
                let known: Vec<String> = paths.iter().flatten().cloned().collect();
                let resolved = !paths.is_empty() && known.len() == paths.len();
                if !resolved {
                    continue;
                }
                let ok = {
                    let mut sc = scope.lock().unwrap();
                    known.iter().all(|p| sc.contains(p).0)
                };
                let folder = if ok { Some(known[0].clone()) } else { None };
                {
                    let mut g = seen.lock().unwrap();
                    if g.get(&hwnd) == Some(&folder) {
                        continue;
                    }
                    g.insert(hwnd, folder.clone());
                }
                crate::diag!("monitor", "폴더 바뀜: hwnd={} → {:?} ({})", hwnd, folder, why);
                emit(MEvent::Folder(hwnd, folder));
            }
            // 다른 범위 탐색기가 오래 앞에 있으면 패널을 그쪽으로
            let w = watch.load(Ordering::SeqCst);
            let fg_folder = fg.and_then(|f| seen.lock().unwrap().get(&f).cloned().flatten());
            match (fg, fg_folder) {
                (Some(f), Some(folder)) if w != 0 && f != w => {
                    if fg_candidate != Some(f) {
                        fg_candidate = Some(f);
                        fg_since = Instant::now();
                    } else if fg_since.elapsed() >= SWITCH_DWELL {
                        fg_candidate = None;
                        crate::diag!("monitor", "패널을 앞의 창으로 옮긴다: hwnd={} → {}", f, folder);
                        emit(MEvent::Folder(f, Some(folder)));
                    }
                }
                _ => fg_candidate = None,
            }
            let mut g = seen.lock().unwrap();
            if g.len() > 16 {
                g.retain(|h, _| win::is_window(*h));
            }
        }
        if last_forget.elapsed() > Duration::from_secs(60) {
            last_forget = Instant::now();
            loc.forget_closed();
        }
        std::thread::sleep(Duration::from_millis(poll_ms));
    }
}
