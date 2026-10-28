//------------------------------------------------------------------
// 트레이 아이콘 (Python tray.py)
//=> 자기 스레드에서 숨은 창 하나와 메시지 펌프를 돌린다. 왼쪽·오른쪽 클릭이면 메뉴를 띄운다.
//   메뉴 맨 위 회색 줄은 지금 상태, 체크·회색 표시는 메인 스레드가 넣어 둔 값을 본다.
//------------------------------------------------------------------

use std::sync::{Arc, Mutex, OnceLock};

use windows::core::{HSTRING, PCWSTR};
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Shell::*;
use windows::Win32::UI::WindowsAndMessaging::*;

const WM_TRAY: u32 = WM_USER + 20;
const WM_UPDATE: u32 = WM_USER + 21;
const WM_NOTIFY_INFO: u32 = WM_USER + 22;

const MENU: &[(u32, &str, &str)] = &[
    (1006, "open", "화면 열기"),
    (1010, "info", "설정"),
    (0, "", ""),
    (1007, "index_now", "지금 인덱싱"),
    (1009, "autoindex", "자동 인덱싱"),
    (0, "", ""),
    (1005, "exit", "종료"),
];

//------------------------------------------------------------------
// 메인 스레드와 나누는 트레이 상태
//------------------------------------------------------------------
#[derive(Default)]
pub struct TrayState {
    pub tooltip: String,
    pub title: String,
    pub autoindex_checked: bool,
    pub index_now_enabled: bool,
    pub balloon: Option<(String, String)>,
}

struct Ctx {
    state: Arc<Mutex<TrayState>>,
    on_action: Box<dyn Fn(&str) + Send + Sync>,
}

static CTX: OnceLock<Ctx> = OnceLock::new();
static HWND_TRAY: std::sync::atomic::AtomicIsize = std::sync::atomic::AtomicIsize::new(0);

//------------------------------------------------------------------
// 트레이 손잡이 — 다른 스레드가 툴팁 갱신·알림을 부탁할 때 쓴다
//------------------------------------------------------------------
pub struct Tray {
    pub state: Arc<Mutex<TrayState>>,
}

impl Tray {
    //------------------------------------------------------------------
    // 시작 — 트레이 스레드를 띄우고 창이 준비될 때까지(최대 5초) 기다린다
    //------------------------------------------------------------------
    pub fn start(on_action: Box<dyn Fn(&str) + Send + Sync>) -> Tray {
        let state = Arc::new(Mutex::new(TrayState { tooltip: "MoAI-S".into(), title: "MoAI-S".into(), ..Default::default() }));
        let _ = CTX.set(Ctx { state: state.clone(), on_action });
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::Builder::new().name("rsb-tray".into()).spawn(move || run(tx)).ok();
        let _ = rx.recv_timeout(std::time::Duration::from_secs(5));
        Tray { state }
    }

    // 툴팁 바꾸기
    pub fn set_tooltip(&self, text: &str) {
        self.state.lock().unwrap().tooltip = text.to_string();
        post(WM_UPDATE);
    }
    pub fn set_title(&self, text: &str) {
        self.state.lock().unwrap().title = text.to_string();
    }
    pub fn set_flags(&self, autoindex: bool, index_now: bool) {
        let mut g = self.state.lock().unwrap();
        g.autoindex_checked = autoindex;
        g.index_now_enabled = index_now;
    }
    // 풍선 알림
    pub fn notify(&self, title: &str, msg: &str) {
        self.state.lock().unwrap().balloon = Some((title.to_string(), msg.to_string()));
        post(WM_NOTIFY_INFO);
    }
    pub fn stop(&self) {
        post(WM_CLOSE);
    }
}

fn post(msg: u32) {
    let h = HWND_TRAY.load(std::sync::atomic::Ordering::SeqCst);
    if h != 0 {
        unsafe {
            let _ = PostMessageW(HWND(h as *mut _), msg, WPARAM(0), LPARAM(0));
        }
    }
}

// 아이콘 — exe 안 리소스 1번(없으면 기본 아이콘)
fn icon() -> HICON {
    unsafe {
        let inst = GetModuleHandleW(None).unwrap_or_default();
        LoadIconW(inst, PCWSTR(1 as *const u16)).or_else(|_| LoadIconW(None, IDI_APPLICATION)).unwrap_or_default()
    }
}

// 글을 고정 길이 UTF-16 칸에 넣기
fn fill<const N: usize>(dst: &mut [u16; N], s: &str) {
    let w: Vec<u16> = s.encode_utf16().take(N - 1).collect();
    dst[..w.len()].copy_from_slice(&w);
    dst[w.len()] = 0;
}

fn nid(hwnd: HWND) -> NOTIFYICONDATAW {
    NOTIFYICONDATAW { cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32, hWnd: hwnd, uID: 0, uCallbackMessage: WM_TRAY, hIcon: icon(), ..Default::default() }
}

// 트레이 스레드 본체
fn run(ready: std::sync::mpsc::Sender<()>) {
    unsafe {
        let inst = GetModuleHandleW(None).unwrap_or_default();
        let cls = HSTRING::from("RagSearchBoxRsTray");
        let wc = WNDCLASSW { lpfnWndProc: Some(wnd_proc), hInstance: inst.into(), lpszClassName: PCWSTR(cls.as_ptr()), ..Default::default() };
        RegisterClassW(&wc);
        let hwnd = CreateWindowExW(WINDOW_EX_STYLE(0), &cls, &HSTRING::from("RAGSearchBox-rs"), WINDOW_STYLE(0), 0, 0, 0, 0, None, None, inst, None).unwrap_or_default();
        HWND_TRAY.store(hwnd.0 as isize, std::sync::atomic::Ordering::SeqCst);
        let mut n = nid(hwnd);
        n.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
        fill(&mut n.szTip, "MoAI-S");
        let _ = Shell_NotifyIconW(NIM_ADD, &n);
        let _ = ready.send(());
        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).as_bool() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}

// 메뉴 띄우기
unsafe fn show_menu(hwnd: HWND) {
    let ctx = match CTX.get() {
        Some(c) => c,
        None => return,
    };
    let (title, autoindex, index_now) = {
        let g = ctx.state.lock().unwrap();
        (g.title.clone(), g.autoindex_checked, g.index_now_enabled)
    };
    let menu = match CreatePopupMenu() {
        Ok(m) => m,
        Err(_) => return,
    };
    let t: String = title.chars().take(80).collect();
    let _ = AppendMenuW(menu, MF_STRING | MF_GRAYED, 1000, &HSTRING::from(t.as_str()));
    let _ = AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null());
    for (id, name, label) in MENU {
        if *id == 0 {
            let _ = AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null());
            continue;
        }
        let mut flags = MF_STRING;
        if *name == "autoindex" && autoindex {
            flags |= MF_CHECKED;
        }
        if *name == "index_now" && !index_now {
            flags |= MF_GRAYED;
        }
        let _ = AppendMenuW(menu, flags, *id as usize, &HSTRING::from(*label));
    }
    let mut p = windows::Win32::Foundation::POINT::default();
    let _ = GetCursorPos(&mut p);
    let _ = SetForegroundWindow(hwnd);
    let _ = TrackPopupMenu(menu, TPM_LEFTALIGN, p.x, p.y, 0, hwnd, None);
    let _ = PostMessageW(hwnd, WM_NULL, WPARAM(0), LPARAM(0));
    let _ = DestroyMenu(menu);
}

unsafe extern "system" fn wnd_proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    match msg {
        WM_TRAY => {
            let ev = lp.0 as u32;
            if ev == WM_RBUTTONUP || ev == WM_LBUTTONUP {
                show_menu(hwnd);
            }
            LRESULT(0)
        }
        WM_UPDATE => {
            if let Some(ctx) = CTX.get() {
                let tip = ctx.state.lock().unwrap().tooltip.clone();
                let mut n = nid(hwnd);
                n.uFlags = NIF_TIP;
                fill(&mut n.szTip, &tip);
                let _ = Shell_NotifyIconW(NIM_MODIFY, &n);
            }
            LRESULT(0)
        }
        WM_NOTIFY_INFO => {
            if let Some(ctx) = CTX.get() {
                let b = ctx.state.lock().unwrap().balloon.take();
                if let Some((title, body)) = b {
                    let mut n = nid(hwnd);
                    n.uFlags = NIF_INFO;
                    fill(&mut n.szInfo, &body);
                    fill(&mut n.szInfoTitle, &title);
                    n.dwInfoFlags = NIIF_INFO;
                    n.Anonymous.uTimeout = 5000;
                    let _ = Shell_NotifyIconW(NIM_MODIFY, &n);
                }
            }
            LRESULT(0)
        }
        WM_COMMAND => {
            let id = (wp.0 & 0xFFFF) as u32;
            if let Some((_, name, _)) = MENU.iter().find(|m| m.0 == id && m.0 != 0) {
                if let Some(ctx) = CTX.get() {
                    (ctx.on_action)(name);
                }
            }
            LRESULT(0)
        }
        WM_CLOSE | WM_DESTROY => {
            let n = nid(hwnd);
            let _ = Shell_NotifyIconW(NIM_DELETE, &n);
            let _ = DestroyWindow(hwnd);
            PostQuitMessage(0);
            LRESULT(0)
        }
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}
