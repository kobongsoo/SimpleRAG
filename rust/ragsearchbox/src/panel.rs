//------------------------------------------------------------------
// 폴더 패널 창 (Python chat_panel.py 의 창 관리 부분)
//=> 탐색기 오른쪽 작업 영역 띠에 붙는 창 하나(WebView2 로 HTML 화면을 그린다).
//    · 뜰 때는 포커스를 뺏지 않는다(SW_SHOWNOACTIVATE + 뺏겼으면 돌려주기)
//    · 자리를 만들려고 탐색기를 왼쪽으로 물리고, 숨을 때 원래대로 돌려준다(사용자가 그 뒤 옮겼으면 그대로)
//    · 사용자가 가장자리를 끌어 너비를 바꾸면 그 너비를 INI 에 저장한다
//    · 닫기(X)를 누르면 그 탐색기 창에서는 범위를 벗어났다 돌아올 때까지 다시 뜨지 않는다
//------------------------------------------------------------------

use std::collections::HashSet;
use std::time::Instant;

use serde_json::{json, Value};
use tao::platform::windows::WindowExtWindows;
use tao::window::Window;
use wry::WebView;

use crate::settings::{self, Settings};
use crate::win;

//------------------------------------------------------------------
// 패널
//------------------------------------------------------------------
pub struct Panel {
    pub window: Window,
    pub webview: WebView,
    pub visible: bool,
    pub folder: Option<String>,
    pub target: isize,
    pub scope_mode: String,
    closed_for: HashSet<isize>,
    saved: Option<(bool, (i32, i32, i32, i32))>,
    room_rect: Option<(i32, i32, i32, i32)>,
    last_rect: Option<(i32, i32, i32, i32)>,
    width: i32,
    saved_width: i32,
    width_save_at: Option<Instant>,
    pub ready: bool,
    queued: Vec<String>,
    // 모니터 배율이 바뀐 시각 — 그 직후 시스템이 바꾼 크기를 사용자 조정으로 오인하지 않게
    pub dpi_changed_at: Option<Instant>,
}

impl Panel {
    pub fn new(window: Window, webview: WebView, s: &Settings) -> Panel {
        Panel {
            window, webview, visible: false, folder: None, target: 0, scope_mode: s.panel_search_scope.clone(),
            closed_for: HashSet::new(), saved: None, room_rect: None, last_rect: None,
            width: s.win_width as i32, saved_width: s.win_width as i32, width_save_at: None, ready: false, queued: vec![], dpi_changed_at: None,
        }
    }

    pub fn hwnd(&self) -> isize {
        self.window.hwnd()
    }

    //------------------------------------------------------------------
    // 화면 함수 부르기 — 화면이 아직 안 떴으면 모아 두었다가 ready 때 보낸다
    //------------------------------------------------------------------
    pub fn js(&mut self, code: String) {
        if self.ready {
            let _ = self.webview.evaluate_script(&code);
        } else {
            self.queued.push(code);
        }
    }
    pub fn call(&mut self, func: &str, arg: Value) {
        self.js(format!("rsb.{}({})", func, arg));
    }
    // 화면을 갈아 끼웠다(탐색기 ↔ RAG) — 새 화면이 ready 를 보낼 때까지 부르는 함수를 모아 둔다
    pub fn reset_ready(&mut self) {
        self.ready = false;
        self.queued.clear();
    }
    pub fn on_ready(&mut self) {
        self.ready = true;
        for c in std::mem::take(&mut self.queued) {
            let _ = self.webview.evaluate_script(&c);
        }
    }

    pub fn set_state(&mut self, t: &str) {
        self.call("setState", json!(t));
    }
    pub fn notice(&mut self, t: &str) {
        self.call("notice", json!(t));
    }
    pub fn show_error(&mut self, t: &str) {
        crate::warn!("panel", "패널에 오류 표시: {}", t);
        self.call("error", json!(t));
    }

    //------------------------------------------------------------------
    // 그 탐색기 창 옆에 띄우기(이미 붙어 있으면 폴더 이름만 바꾼다)
    //------------------------------------------------------------------
    pub fn show_for(&mut self, hwnd: isize, folder: &str, s: &Settings) {
        if self.closed_for.contains(&hwnd) {
            return;
        }
        let same = self.visible && self.target == hwnd;
        self.folder = Some(folder.to_string());
        self.call("setFolder", json!(folder));
        if same {
            return;
        }
        if self.visible && self.target != hwnd {
            self.give_back();
        }
        self.target = hwnd;
        self.place(true, s);
        let prev = win::foreground();
        win::show_no_activate(self.hwnd());
        win::give_focus_back(self.hwnd(), prev);
        self.visible = true;
        crate::info!("panel", "패널 표시: {} (창 {})", folder, hwnd);
    }

    //------------------------------------------------------------------
    // 숨기기 — 탐색기 자리를 돌려준다
    //------------------------------------------------------------------
    pub fn hide(&mut self, give_back: bool) {
        if !self.visible {
            return;
        }
        if give_back {
            self.give_back();
        }
        self.visible = false;
        win::hide(self.hwnd());
        crate::info!("panel", "패널 숨김");
    }

    // 사용자가 닫기를 눌렀다
    pub fn close_by_user(&mut self) {
        if self.target != 0 {
            self.closed_for.insert(self.target);
        }
        self.hide(true);
    }

    pub fn forget_closed(&mut self, hwnd: isize) {
        if self.closed_for.remove(&hwnd) {
            crate::info!("panel", "닫았던 창 {} 에서 다시 띄울 수 있게 했다", hwnd);
        }
    }

    //------------------------------------------------------------------
    // 탐색기 따라가기(50ms 마다) — 닫히면 숨고, 최소화면 감추고, 옮기면 따라간다
    //------------------------------------------------------------------
    pub fn follow(&mut self, s: &mut Settings) {
        if !self.visible || self.target == 0 {
            return;
        }
        let (alive, minimized, _) = win::target_state(self.target);
        if !alive {
            self.saved = None;
            self.hide(false);
            return;
        }
        if minimized {
            if win::is_visible(self.hwnd()) {
                win::hide(self.hwnd());
            }
            return;
        }
        if !win::is_visible(self.hwnd()) {
            let prev = win::foreground();
            win::show_no_activate(self.hwnd());
            win::give_focus_back(self.hwnd(), prev);
        }
        if win::mouse_down() {
            return;
        }
        self.adopt_user_size(s);
        self.place(false, s);
    }

    // 사용자가 너비를 바꿨으면 받아들이고 저장한다
    fn adopt_user_size(&mut self, s: &mut Settings) {
        let (l, _, r, _) = match win::window_rect(self.hwnd()) {
            Some(x) => x,
            None => return,
        };
        // 배율이 막 바뀌었으면 시스템이 크기를 바꾼 것이다 — 다시 자리만 잡는다
        if self.dpi_changed_at.map(|t| t.elapsed().as_secs_f64() < 1.5).unwrap_or(false) {
            self.last_rect = None;
            return;
        }
        let width = r - l;
        let scale = win::dpi_scale(self.target);
        let placed = self.last_rect.map(|x| x.2).unwrap_or((self.width as f64 * scale) as i32);
        if (width - placed).abs() <= 4 {
            return;
        }
        let work = win::work_area(self.target);
        let phys = 260.max(width.min((work.2 - work.0) / 2));
        self.width = (phys as f64 / scale).round() as i32;
        crate::info!("panel", "패널 너비를 {}px 로 바꿨다 — 탐색기 자리를 다시 만든다", self.width);
        self.last_rect = None;
        self.remember_width(s);
        if s.shrink_explorer && self.target != 0 {
            let region = win::left_region(work, (self.width as f64 * scale) as i32);
            if let (_, _, Some(rect)) = win::target_state(self.target) {
                if !win::fits_in(rect, region) && win::make_room(self.target, region) {
                    self.room_rect = win::target_state(self.target).2;
                }
            }
        }
    }

    // 너비를 INI 에 저장(2초에 한 번)
    fn remember_width(&mut self, s: &mut Settings) {
        if (self.width - self.saved_width).abs() <= 4 {
            return;
        }
        if self.width_save_at.map(|t| t.elapsed().as_secs_f64() < 2.0).unwrap_or(false) {
            return;
        }
        self.width_save_at = Some(Instant::now());
        match settings::save_window_width(&s.ini_path, self.width as i64) {
            Ok(_) => {
                self.saved_width = self.width;
                s.win_width = self.width as i64;
                crate::info!("panel", "패널 너비 {}px 를 설정에 저장했다", self.width);
            }
            Err(e) => crate::diag!("panel", "너비를 저장하지 못했다: {}", e),
        }
    }

    //------------------------------------------------------------------
    // 자리 잡기 — 오른쪽 띠에 놓고, 처음이면 탐색기를 왼쪽으로 물린다
    //------------------------------------------------------------------
    fn place(&mut self, first: bool, s: &Settings) {
        let work = win::work_area(self.target);
        // INI 너비는 100% 배율 기준 — 그 모니터 배율을 곱한 물리 픽셀로 놓는다
        let phys = (self.width as f64 * win::dpi_scale(self.target)) as i32;
        let strip = win::right_strip(work, phys);
        let region = win::left_region(work, phys);
        if first && s.shrink_explorer {
            self.saved = win::snapshot(self.target);
            match win::target_state(self.target).2 {
                Some(rect) if !win::fits_in(rect, region) => {
                    if win::make_room(self.target, region) {
                        self.room_rect = win::target_state(self.target).2;
                        crate::info!("panel", "탐색기를 왼쪽으로 물렸다: {:?}", self.room_rect);
                    }
                }
                _ => self.saved = None,
            }
        }
        let drifted = match (win::window_rect(self.hwnd()), self.last_rect) {
            (Some((l, t, r, b)), Some(_)) => (l - strip.0).abs() > 4 || (t - strip.1).abs() > 4 || ((r - l) - strip.2).abs() > 4 || ((b - t) - strip.3).abs() > 4,
            _ => false,
        };
        if Some(strip) != self.last_rect || drifted {
            self.last_rect = Some(strip);
            win::place_above_target(self.hwnd(), self.target, strip);
        }
    }

    // 물렸던 탐색기를 되돌린다
    fn give_back(&mut self) {
        if self.saved.is_some() && self.target != 0 && win::restore(self.target, self.saved, self.room_rect) {
            crate::info!("panel", "탐색기 창을 원래대로 돌려줬다");
        }
        self.saved = None;
        self.room_rect = None;
        self.last_rect = None;
    }

    //------------------------------------------------------------------
    // 앞으로 가져와 입력 칸에 포커스(트레이 '화면 열기')
    //------------------------------------------------------------------
    pub fn activate(&mut self) {
        if !self.visible {
            return;
        }
        let ok = win::bring_to_front(self.hwnd());
        self.js("rsb.focusInput()".into());
        crate::info!("panel", "패널을 앞으로 가져왔다: {}", if ok { "성공" } else { "실패(Windows 가 막음)" });
    }

    pub fn close(&mut self) {
        self.give_back();
        self.visible = false;
        win::hide(self.hwnd());
    }
}
