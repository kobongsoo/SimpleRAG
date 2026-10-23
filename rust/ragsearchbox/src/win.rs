//------------------------------------------------------------------
// Win32 도우미 — 창 위치·도킹·전경 창·파일 열기·자동 시작·작업 개체
//=> Python 판의 dock.py · autorun.py · chat_panel 의 창 조작 함수들을 모았다.
//------------------------------------------------------------------

use windows::core::{HSTRING, PCWSTR};
use windows::Win32::Foundation::{CloseHandle, BOOL, HANDLE, HWND, LPARAM, POINT, RECT};
use windows::Win32::Graphics::Gdi::{GetMonitorInfoW, MonitorFromPoint, MonitorFromWindow, MONITORINFO, MONITOR_DEFAULTTONEAREST};
use windows::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
use windows::Win32::UI::Input::KeyboardAndMouse::GetAsyncKeyState;
use windows::Win32::UI::Shell::ShellExecuteW;
use windows::Win32::UI::WindowsAndMessaging::*;

pub const GAP: i32 = 6;
const MIN_HEIGHT: i32 = 160;

pub fn h(x: isize) -> HWND {
    HWND(x as *mut _)
}
pub fn hv(w: HWND) -> isize {
    w.0 as isize
}

//------------------------------------------------------------------
// 창 클래스 이름
//------------------------------------------------------------------
pub fn class_name(w: isize) -> String {
    let mut buf = [0u16; 256];
    let n = unsafe { GetClassNameW(h(w), &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

// 창 제목
pub fn window_text(w: isize) -> String {
    let mut buf = [0u16; 512];
    let n = unsafe { GetWindowTextW(h(w), &mut buf) };
    String::from_utf16_lossy(&buf[..n.max(0) as usize])
}

pub fn is_window(w: isize) -> bool {
    w != 0 && unsafe { IsWindow(h(w)).as_bool() }
}

//------------------------------------------------------------------
// 지금 전경 창이 탐색기면 그 손잡이
//------------------------------------------------------------------
pub fn foreground() -> isize {
    hv(unsafe { GetForegroundWindow() })
}
pub fn foreground_explorer() -> Option<isize> {
    let f = foreground();
    if f != 0 && class_name(f) == "CabinetWClass" {
        Some(f)
    } else {
        None
    }
}

pub fn window_rect(w: isize) -> Option<(i32, i32, i32, i32)> {
    let mut r = RECT::default();
    unsafe { GetWindowRect(h(w), &mut r) }.ok()?;
    Some((r.left, r.top, r.right, r.bottom))
}

//------------------------------------------------------------------
// 대상 창 상태 — (살아 있나, 최소화·숨김인가, 위치)
//------------------------------------------------------------------
pub fn target_state(w: isize) -> (bool, bool, Option<(i32, i32, i32, i32)>) {
    if !is_window(w) {
        return (false, false, None);
    }
    unsafe {
        if IsIconic(h(w)).as_bool() || !IsWindowVisible(h(w)).as_bool() {
            return (true, true, None);
        }
    }
    (true, false, window_rect(w))
}

//------------------------------------------------------------------
// 그 창이 있는 모니터의 배율(96 DPI = 1.0) — INI 너비는 100% 기준 값이라 곱해서 쓴다
//------------------------------------------------------------------
pub fn dpi_scale(w: isize) -> f64 {
    let d = unsafe { windows::Win32::UI::HiDpi::GetDpiForWindow(h(w)) };
    if d == 0 { 1.0 } else { d as f64 / 96.0 }
}

// 모니터 작업 영역(작업 표시줄 제외)
fn work_of(mon: windows::Win32::Graphics::Gdi::HMONITOR) -> (i32, i32, i32, i32) {
    let mut mi = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
    if unsafe { GetMonitorInfoW(mon, &mut mi) }.as_bool() {
        let r = mi.rcWork;
        (r.left, r.top, r.right, r.bottom)
    } else {
        (0, 0, 1920, 1080)
    }
}
pub fn work_area(w: isize) -> (i32, i32, i32, i32) {
    work_of(unsafe { MonitorFromWindow(h(w), MONITOR_DEFAULTTONEAREST) })
}
pub fn work_area_at(x: i32, y: i32) -> (i32, i32, i32, i32) {
    work_of(unsafe { MonitorFromPoint(POINT { x, y }, MONITOR_DEFAULTTONEAREST) })
}
pub fn cursor_pos() -> (i32, i32) {
    let mut p = POINT::default();
    let _ = unsafe { GetCursorPos(&mut p) };
    (p.x, p.y)
}

//------------------------------------------------------------------
// 작업 영역 오른쪽 띠(패널 자리) — 화면 절반은 넘지 않는다
//------------------------------------------------------------------
pub fn right_strip(work: (i32, i32, i32, i32), width: i32) -> (i32, i32, i32, i32) {
    let (kl, kt, kr, kb) = work;
    let w = 200.max(width.min((kr - kl) / 2));
    (kr - w, kt, w, (kb - kt).max(MIN_HEIGHT))
}
// 그 왼쪽 나머지(탐색기가 들어갈 자리)
pub fn left_region(work: (i32, i32, i32, i32), width: i32) -> (i32, i32, i32, i32) {
    let (kl, kt, _, kb) = work;
    let (x, _, _, _) = right_strip(work, width);
    (kl, kt, x - GAP, kb)
}
pub fn fits_in(r: (i32, i32, i32, i32), region: (i32, i32, i32, i32)) -> bool {
    r.0 >= region.0 - 2 && r.1 >= region.1 - 2 && r.2 <= region.2 + 2 && r.3 <= region.3 + 2
}

//------------------------------------------------------------------
// 창 상태 사진(되돌리기용) — (최대화였나, 위치)
//------------------------------------------------------------------
pub fn snapshot(w: isize) -> Option<(bool, (i32, i32, i32, i32))> {
    let mut wp = WINDOWPLACEMENT { length: std::mem::size_of::<WINDOWPLACEMENT>() as u32, ..Default::default() };
    unsafe { GetWindowPlacement(h(w), &mut wp) }.ok()?;
    Some((wp.showCmd == SW_SHOWMAXIMIZED.0 as u32, window_rect(w)?))
}

//------------------------------------------------------------------
// 탐색기를 왼쪽 자리 안으로 물리기 — 최대화면 풀고 자리 전체로
// -out: 옮겼으면 true
//------------------------------------------------------------------
pub fn make_room(w: isize, region: (i32, i32, i32, i32)) -> bool {
    let (rl, rt, rr, rb) = region;
    let was_max = snapshot(w).map(|s| s.0).unwrap_or(false);
    unsafe {
        if was_max {
            let _ = ShowWindow(h(w), SW_RESTORE);
        }
        let (l, t, r, b) = match window_rect(w) {
            Some(x) => x,
            None => return false,
        };
        let (nl, nt, nw, nh) = if was_max {
            (rl, rt, rr - rl, rb - rt)
        } else {
            let nw = (r - l).min(rr - rl);
            let nh = (b - t).min(rb - rt);
            let nl = l.max(rl).min(rr - nw);
            let nt = t.max(rt).min(rb - nh);
            if (nl, nt, nw, nh) == (l, t, r - l, b - t) {
                return false;
            }
            (nl, nt, nw, nh)
        };
        MoveWindow(h(w), nl, nt, nw, nh, true).is_ok()
    }
}

//------------------------------------------------------------------
// 물렸던 탐색기 되돌리기 — 사용자가 그 뒤에 옮겼으면 그대로 둔다
//------------------------------------------------------------------
pub fn restore(w: isize, saved: Option<(bool, (i32, i32, i32, i32))>, ours: Option<(i32, i32, i32, i32)>) -> bool {
    let (max, rect) = match saved {
        Some(s) => s,
        None => return false,
    };
    if !is_window(w) {
        return false;
    }
    if let (Some(now), Some(o)) = (window_rect(w), ours) {
        let d = [(now.0 - o.0), (now.1 - o.1), (now.2 - o.2), (now.3 - o.3)];
        if d.iter().any(|x| x.abs() > 4) {
            return false;
        }
    }
    unsafe {
        if max {
            let _ = ShowWindow(h(w), SW_MAXIMIZE);
        } else {
            let _ = MoveWindow(h(w), rect.0, rect.1, rect.2 - rect.0, rect.3 - rect.1, true);
        }
    }
    true
}

//------------------------------------------------------------------
// 패널을 탐색기 바로 위(z 순서)에 놓기 — 활성화하지 않는다
//------------------------------------------------------------------
pub fn place_above_target(panel: isize, target: isize, rect: (i32, i32, i32, i32)) {
    let (x, y, w, ht) = rect;
    unsafe {
        let above = GetWindow(h(target), GW_HWNDPREV).map(hv).unwrap_or(0);
        let mut flags = SWP_NOACTIVATE;
        let after = if above == panel {
            flags |= SWP_NOZORDER;
            HWND::default()
        } else if above != 0 {
            h(above)
        } else {
            HWND_TOP
        };
        let _ = SetWindowPos(h(panel), after, x, y, w, ht, flags);
    }
}

//------------------------------------------------------------------
// 활성화하지 않고 보이기 / 숨기기
//------------------------------------------------------------------
pub fn show_no_activate(w: isize) {
    unsafe {
        let _ = ShowWindow(h(w), SW_SHOWNOACTIVATE);
    }
}
pub fn hide(w: isize) {
    unsafe {
        let _ = ShowWindow(h(w), SW_HIDE);
    }
}
pub fn is_visible(w: isize) -> bool {
    unsafe { IsWindowVisible(h(w)).as_bool() }
}

//------------------------------------------------------------------
// 창을 확실히 맨 앞으로 — 전경 스레드에 잠깐 붙어 SetForegroundWindow 를 허락받는다
//------------------------------------------------------------------
pub fn bring_to_front(w: isize) -> bool {
    unsafe {
        let fg = GetForegroundWindow();
        let fg_tid = GetWindowThreadProcessId(fg, None);
        let me = GetCurrentThreadId();
        let attached = fg_tid != 0 && fg_tid != me && AttachThreadInput(me, fg_tid, true).as_bool();
        let _ = ShowWindow(h(w), SW_SHOW);
        let _ = BringWindowToTop(h(w));
        let ok = SetForegroundWindow(h(w)).as_bool();
        if attached {
            let _ = AttachThreadInput(me, fg_tid, false);
        }
        ok
    }
}

// 뺏은 포커스를 원래 창에 돌려준다
pub fn give_focus_back(panel: isize, prev: isize) {
    if prev != 0 && prev != panel && foreground() == panel {
        unsafe {
            let _ = SetForegroundWindow(h(prev));
        }
    }
}

// 왼쪽 단추가 눌려 있나(사용자가 끄는 중이면 따라가지 않는다)
pub fn mouse_down() -> bool {
    unsafe { (GetAsyncKeyState(0x01) as u16 & 0x8000) != 0 }
}

//------------------------------------------------------------------
// 파일·폴더 열기(연결 프로그램) — os.startfile
//------------------------------------------------------------------
pub fn open_path(path: &str) -> Result<(), String> {
    let r = unsafe { ShellExecuteW(HWND::default(), &HSTRING::from("open"), &HSTRING::from(path), PCWSTR::null(), PCWSTR::null(), SW_SHOWNORMAL) };
    if r.0 as isize > 32 {
        Ok(())
    } else {
        Err(format!("오류 코드 {}", r.0 as isize))
    }
}

// 탐색기로 폴더 열기
pub fn open_explorer(path: &str) -> Result<(), String> {
    std::process::Command::new("explorer.exe").arg(path).spawn().map(|_| ()).map_err(|e| e.to_string())
}

//------------------------------------------------------------------
// 보이는 최상위 창을 z 순서대로 — (손잡이)
//------------------------------------------------------------------
pub fn enum_windows() -> Vec<isize> {
    unsafe extern "system" fn cb(w: HWND, l: LPARAM) -> BOOL {
        let v = &mut *(l.0 as *mut Vec<isize>);
        v.push(hv(w));
        BOOL(1)
    }
    let mut v: Vec<isize> = vec![];
    unsafe {
        let _ = EnumWindows(Some(cb), LPARAM(&mut v as *mut _ as isize));
    }
    v
}

//------------------------------------------------------------------
// 탐색기 창의 탭 제목들(ShellTabWindowClass 자식)
//------------------------------------------------------------------
pub fn tab_titles(w: isize) -> Vec<String> {
    let mut out = vec![];
    unsafe {
        let mut child = FindWindowExW(h(w), HWND::default(), &HSTRING::from("ShellTabWindowClass"), PCWSTR::null()).unwrap_or_default();
        while !child.0.is_null() {
            out.push(window_text(hv(child)));
            child = FindWindowExW(h(w), child, &HSTRING::from("ShellTabWindowClass"), PCWSTR::null()).unwrap_or_default();
        }
    }
    out
}

//------------------------------------------------------------------
// 한 번만 실행 — 이름 붙은 뮤텍스(이미 있으면 None)
//------------------------------------------------------------------
pub fn single_instance(name: &str) -> Option<HANDLE> {
    use windows::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows::Win32::System::Threading::CreateMutexW;
    unsafe {
        let hnd = CreateMutexW(None, false, &HSTRING::from(name)).ok()?;
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = CloseHandle(hnd);
            return None;
        }
        Some(hnd)
    }
}

//------------------------------------------------------------------
// 끝날 때 자식(워커)도 함께 죽게 하는 작업 개체
//------------------------------------------------------------------
pub fn create_kill_job() -> Option<HANDLE> {
    use windows::Win32::System::JobObjects::*;
    unsafe {
        let job = CreateJobObjectW(None, PCWSTR::null()).ok()?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let _ = SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info as *const _ as *const _,
                                        std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32);
        Some(job)
    }
}
pub fn assign_job(job: HANDLE, child: &std::process::Child) {
    use std::os::windows::io::AsRawHandle;
    use windows::Win32::System::JobObjects::AssignProcessToJobObject;
    unsafe {
        let _ = AssignProcessToJobObject(job, HANDLE(child.as_raw_handle() as *mut _));
    }
}

// ── 자동 시작(레지스트리 Run) ────────────────────────────

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
pub const RUN_NAME: &str = "RAGSearchBox-rs";

//------------------------------------------------------------------
// 등록된 값(없으면 None)
//------------------------------------------------------------------
pub fn autorun_value() -> Option<String> {
    use windows::Win32::System::Registry::*;
    unsafe {
        let mut hk = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, &HSTRING::from(RUN_KEY), 0, KEY_READ, &mut hk).is_err() {
            return None;
        }
        let mut buf = vec![0u16; 2048];
        let mut size = (buf.len() * 2) as u32;
        let r = RegQueryValueExW(hk, &HSTRING::from(RUN_NAME), None, None, Some(buf.as_mut_ptr() as *mut u8), Some(&mut size));
        let _ = RegCloseKey(hk);
        if r.is_err() {
            return None;
        }
        let n = (size as usize / 2).saturating_sub(1);
        Some(String::from_utf16_lossy(&buf[..n.min(buf.len())]))
    }
}
pub fn autorun_command() -> String {
    format!("\"{}\"", std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default())
}
pub fn autorun_enable() -> Result<String, String> {
    use windows::Win32::System::Registry::*;
    let v = autorun_command();
    unsafe {
        let mut hk = HKEY::default();
        RegCreateKeyExW(HKEY_CURRENT_USER, &HSTRING::from(RUN_KEY), 0, PCWSTR::null(), REG_OPTION_NON_VOLATILE, KEY_SET_VALUE, None, &mut hk, None)
            .ok().map_err(|e| format!("자동 시작을 등록하지 못했습니다: {}", e))?;
        let w: Vec<u16> = v.encode_utf16().chain(Some(0)).collect();
        let bytes = std::slice::from_raw_parts(w.as_ptr() as *const u8, w.len() * 2);
        let r = RegSetValueExW(hk, &HSTRING::from(RUN_NAME), 0, REG_SZ, Some(bytes));
        let _ = RegCloseKey(hk);
        r.ok().map_err(|e| format!("자동 시작을 등록하지 못했습니다: {}", e))?;
    }
    Ok(v)
}
pub fn autorun_disable() -> Result<String, String> {
    use windows::Win32::System::Registry::*;
    unsafe {
        let mut hk = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, &HSTRING::from(RUN_KEY), 0, KEY_SET_VALUE, &mut hk).is_err() {
            return Ok("자동 시작이 등록돼 있지 않았습니다".into());
        }
        let r = RegDeleteValueW(hk, &HSTRING::from(RUN_NAME));
        let _ = RegCloseKey(hk);
        if r.is_err() {
            if autorun_value().is_none() {
                return Ok("자동 시작이 등록돼 있지 않았습니다".into());
            }
            return Err(format!("자동 시작을 끄지 못했습니다: {:?}", r));
        }
    }
    Ok("자동 시작을 껐습니다".into())
}
// 프로그램 폴더를 옮겼으면 등록 경로를 고친다
pub fn autorun_fix_path() -> Option<String> {
    let cur = autorun_value()?;
    let want = autorun_command();
    if cur.trim().to_lowercase() == want.trim().to_lowercase() {
        return None;
    }
    autorun_enable().ok()
}
