//------------------------------------------------------------------
// MoAI 탐색기 창 (폴더 아이콘 · 탐색기 모드 설계서 §10 · §14)
//=> 탐색기 모드의 독립 창. 화면(ui/browse.html, LAYOUT=window)은 목록·탐색 창·주소줄을 그리고,
//   미리 보기 창의 PDF 는 이 창 위에 따로 띄운 두 번째 WebView(Edge PDF 뷰어)가 그린다.
//   화면이 미리 보기 자리(#pvframe)를 알려 주면(pv_rect) 그 자리에 맞춘다.
//   (화면 WebView 안의 iframe 으로는 file:// PDF 가 막혀 별도 WebView 로 둔다.)
//   기존 explorer.rs 는 Windows 탐색기 감시라 이름을 browse 로 나눴다.
//------------------------------------------------------------------

use serde_json::{json, Value};
use tao::platform::windows::WindowExtWindows;
use tao::window::Window;
use wry::dpi::{LogicalPosition, LogicalSize};
use wry::{Rect, WebView};

//------------------------------------------------------------------
// 창 상태
// -필드: window·webview = 창과 목록 화면 / preview = PDF 미리 보기 WebView
//        ready·queued = 화면이 뜨기 전 부른 함수를 모아 둔다 / pv_url = 지금 미리 보기 주소
//        pv_rect = 미리 보기 자리(논리 픽셀) / pv_show = 미리 보기 창이 켜져 있나 / pv_pdf = 지금 PDF 를 보이는 중인가
//------------------------------------------------------------------
pub struct Browse {
    pub window: Window,
    pub webview: WebView,
    pub preview: Option<WebView>,
    pub visible: bool,
    ready: bool,
    queued: Vec<String>,
    pv_url: String,
    pv_rect: (f64, f64, f64, f64),
    pv_show: bool,
    pv_pdf: bool,
    placed: bool,
    // 기억한 창 자리(x, y 는 물리 픽셀, 안쪽 너비 · 높이는 논리 픽셀, 최대화) — 숨길 때 잰다. 처음 보일 때 크기 · 최대화만 쓴다(위치는 쓰지 않음)
    pub place: Option<(i32, i32, u32, u32, bool)>,
}

impl Browse {
    pub fn new(window: Window, webview: WebView, preview: Option<WebView>) -> Browse {
        Browse { window, webview, preview, visible: false, ready: false, queued: vec![], pv_url: String::new(),
                 pv_rect: (0.0, 0.0, 0.0, 0.0), pv_show: true, pv_pdf: false, placed: false, place: None }
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
    pub fn is_ready(&self) -> bool {
        self.ready
    }
    pub fn on_ready(&mut self) {
        self.ready = true;
        for c in std::mem::take(&mut self.queued) {
            let _ = self.webview.evaluate_script(&c);
        }
    }

    //------------------------------------------------------------------
    // 보이기 — 최소화돼 있으면 되살리고 앞으로
    //------------------------------------------------------------------
    pub fn show(&mut self) {
        if !self.placed {
            self.placed = true;
            // 지난 크기만 되살린다 — 위치는 기억하지 않고 Windows 가 고른 자리를 화면 안으로(창을 잃어버리지 않게)
            let max = self.restore_size();
            self.fit_on_screen();
            if max {
                self.window.set_maximized(true);
            }
        }
        self.window.set_visible(true);
        self.window.set_minimized(false);
        self.window.set_focus();
        self.visible = true;
        crate::win::bring_to_front(self.hwnd());
    }

    //------------------------------------------------------------------
    // 처음 보일 때 창을 화면 안으로 — Windows 가 고른 자리에 그대로 두면 아래가 작업 표시줄 뒤로 가려
    // 맨 아래 상태줄(모델 준비 · 인덱싱 진행)이 안 보인다. 그래도 넘치면 높이를 줄인다
    //------------------------------------------------------------------
    fn fit_on_screen(&self) {
        let (Some(m), Ok(pos)) = (self.window.current_monitor(), self.window.outer_position()) else { return };
        let outer = self.window.outer_size();
        let inner = self.window.inner_size();
        let top = m.position().y;
        // 작업 표시줄 자리
        let limit = top + m.size().height as i32 - (56.0 * m.scale_factor()) as i32;
        let room = (limit - top).max(0) as u32;
        let height = outer.height.min(room);
        if height < outer.height {
            self.window.set_inner_size(tao::dpi::PhysicalSize::new(inner.width, inner.height - (outer.height - height)));
        }
        // 오른쪽으로 넘치면 왼쪽으로 당긴다(지난 크기가 이 모니터보다 넓었을 때)
        let (left, right) = (m.position().x, m.position().x + m.size().width as i32);
        let x = if pos.x + outer.width as i32 > right { (right - outer.width as i32).max(left) } else { pos.x };
        if pos.y + height as i32 > limit || x != pos.x {
            self.window.set_outer_position(tao::dpi::PhysicalPosition::new(x, (limit - height as i32).max(top).min(pos.y)));
        }
    }

    pub fn hide(&mut self) {
        self.measure_place();
        self.window.set_visible(false);
        self.visible = false;
    }

    //------------------------------------------------------------------
    // 기억한 크기로 — 지금 모니터보다 넓으면 모니터 너비로 줄인다. 돌려줌: 최대화였나
    //=> 논리 픽셀로 기억하므로 배율이 다른 모니터(200% ↔ 100%)에서 열어도 보이는 크기가 같다
    //------------------------------------------------------------------
    fn restore_size(&self) -> bool {
        let Some((_, _, w, h, max)) = self.place else { return false };
        let w = match self.window.current_monitor() {
            Some(m) => (w as f64).min(m.size().width as f64 / m.scale_factor()),
            None => w as f64,
        };
        self.window.set_inner_size(tao::dpi::LogicalSize::new(w, h as f64));
        max
    }

    //------------------------------------------------------------------
    // 지금 자리 재기 — 최대화 · 최소화 중이면 크기는 지난 보통 자리를 그대로 두고 최대화 여부만 바꾼다
    //------------------------------------------------------------------
    fn measure_place(&mut self) {
        if !self.visible || self.window.is_minimized() {
            return;
        }
        let max = self.window.is_maximized();
        if max {
            if let Some(p) = self.place.as_mut() {
                p.4 = true;
            } else if let Ok(pos) = self.window.outer_position() {
                // 처음부터 최대화만 써 왔다 — 되돌릴 크기는 처음 크기로
                self.place = Some((pos.x, pos.y, 1240, 780, true));
            }
            return;
        }
        if let Ok(pos) = self.window.outer_position() {
            // 물리 픽셀 → 논리 픽셀 — 지금 모니터 배율로 나눈다
            let s = self.window.inner_size().to_logical::<f64>(self.window.scale_factor());
            self.place = Some((pos.x, pos.y, s.width.round() as u32, s.height.round() as u32, false));
        }
    }

    // INI 에 적을 글 "x,y,너비,높이,최대화(0/1),L" — 끝의 L 은 논리 픽셀 표시. 아직 잰 적이 없으면 None
    pub fn place_text(&self) -> Option<String> {
        self.place.map(|(x, y, w, h, m)| format!("{},{},{},{},{},L", x, y, w, h, m as u8))
    }

    // INI 글 → 자리 — 모양이 틀리거나 너무 작으면 None
    pub fn parse_place(raw: &str) -> Option<(i32, i32, u32, u32, bool)> {
        let v: Vec<&str> = raw.split(',').map(|t| t.trim()).collect();
        // L 이 없는 값은 물리 픽셀로 적던 이전 빌드의 것이라 버린다(배율이 다르면 크기가 두 배 · 절반이 된다)
        if v.len() != 6 || v[5] != "L" {
            return None;
        }
        let (x, y, w, h) = (v[0].parse().ok()?, v[1].parse().ok()?, v[2].parse::<u32>().ok()?, v[3].parse::<u32>().ok()?);
        if w < 400 || h < 300 {
            return None;
        }
        Some((x, y, w, h, v[4] == "1"))
    }

    //------------------------------------------------------------------
    // PDF 미리 보기 — 그 쪽으로 이동(같은 문서면 #page 만 바뀐다)
    //------------------------------------------------------------------
    pub fn preview_pdf(&mut self, path: &str, page: i64) {
        let url = file_url(path, Some(page));
        if let Some(pv) = &self.preview {
            if url != self.pv_url {
                let same_doc = self.pv_url.split('#').next() == url.split('#').next();
                // 같은 문서에서 #page 만 바꾸면 Edge PDF 뷰어가 쪽을 옮기지 않는다(실측) — 주소를 바꾸고 다시 읽힌다
                let r = if same_doc {
                    pv.evaluate_script(&format!("location.replace({}); location.reload();", json!(url)))
                } else {
                    pv.load_url(&url)
                };
                if let Err(e) = r {
                    crate::warn!("browse", "미리 보기를 열지 못했다: {} ({})", path, e);
                }
                self.pv_url = url;
            }
            self.pv_pdf = true;
            let _ = pv.set_visible(self.pv_show && self.pv_rect.2 > 0.0);
        }
    }

    // 글 미리 보기로 바뀌었다 — PDF WebView 를 숨긴다(화면이 글을 직접 보인다)
    pub fn preview_hide(&mut self) {
        self.pv_pdf = false;
        if let Some(pv) = &self.preview {
            let _ = pv.set_visible(false);
        }
    }

    //------------------------------------------------------------------
    // 모니터 배율이 바뀌었다(배율이 다른 모니터로 창을 옮김) — PDF 미리 보기 자리를 새 배율로 다시 맞춘다
    //=> 미리 보기 WebView 는 자리를 정할 때의 배율로 물리 픽셀을 박아 두므로(wry set_bounds) 그대로 두면
    //   200% → 100% 로 옮길 때 두 배 크기로 목록을 덮고, 반대면 절반 크기로 줄어든다
    //------------------------------------------------------------------
    pub fn dpi_changed(&mut self) {
        let (x, y, w, h) = self.pv_rect;
        if w > 0.0 {
            self.set_preview_rect(x, y, w, h, self.pv_show);
        }
        // 창 크기가 바뀐 뒤 화면이 잰 자리로 한 번 더 — 배율 사건은 크기 바뀜보다 먼저 온다
        self.js("typeof sendRect === 'function' && setTimeout(sendRect, 150)".into());
    }

    //------------------------------------------------------------------
    // 미리 보기 자리 맞추기 — 화면이 #pvframe 의 위치·크기(CSS 픽셀 = 논리 픽셀)를 알려 준다
    //------------------------------------------------------------------
    pub fn set_preview_rect(&mut self, x: f64, y: f64, w: f64, h: f64, show: bool) {
        self.pv_rect = (x, y, w, h);
        self.pv_show = show;
        if let Some(pv) = &self.preview {
            let _ = pv.set_bounds(Rect { position: LogicalPosition::new(x, y).into(), size: LogicalSize::new(w.max(1.0), h.max(1.0)).into() });
            let _ = pv.set_visible(show && self.pv_pdf && w > 0.0);
        }
    }
}

//------------------------------------------------------------------
// 파일 경로 → file:/// 주소 (+ #page=N)
//=> 한글·공백·# 같은 글자는 %인코딩해야 WebView 가 파일을 찾는다. \ 는 / 로.
//   \\서버\공유 는 file://서버/공유 로.
//------------------------------------------------------------------
pub fn file_url(path: &str, page: Option<i64>) -> String {
    use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS};
    const SET: &AsciiSet = &CONTROLS.add(b' ').add(b'#').add(b'%').add(b'?').add(b'"').add(b'<').add(b'>').add(b'`').add(b'{').add(b'}');
    let p = path.replace('\\', "/");
    let enc = utf8_percent_encode(&p, SET).to_string();
    let base = if let Some(unc) = enc.strip_prefix("//") { format!("file://{}", unc) } else { format!("file:///{}", enc) };
    match page {
        Some(n) => format!("{}#page={}", base, n),
        None => base,
    }
}

//------------------------------------------------------------------
// 인덱싱 폴더 목록 → 탐색 창용 [{path, name, drive}]
//------------------------------------------------------------------
pub fn roots_json(folders: &[String]) -> Value {
    Value::Array(folders.iter().map(|f| {
        let drive = crate::folder_icon::drive_letter(f).is_some();
        let name = if drive {
            format!("로컬 디스크 ({}:)", crate::folder_icon::drive_letter(f).unwrap())
        } else {
            f.trim_end_matches(['\\', '/']).rsplit(['\\', '/']).next().unwrap_or(f).to_string()
        };
        json!({"path": f, "name": name, "drive": drive})
    }).collect())
}

//------------------------------------------------------------------
// 하위 폴더 이름(탐색 창 펼치기) — 숨김·시스템 폴더는 뺀다, 이름순 200개까지
//------------------------------------------------------------------
pub fn list_dirs(path: &str) -> Vec<String> {
    use std::os::windows::fs::MetadataExt;
    let mut out: Vec<String> = match std::fs::read_dir(path) {
        Ok(rd) => rd.filter_map(|e| e.ok()).filter(|e| {
            e.metadata().map(|m| m.is_dir() && m.file_attributes() & (0x2 | 0x4) == 0).unwrap_or(false)
        }).map(|e| e.file_name().to_string_lossy().into_owned()).collect(),
        Err(_) => vec![],
    };
    out.sort_by_key(|s| s.to_lowercase());
    out.truncate(200);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    // 이 PC 의 기본 PDF 앱과 쪽 인자를 보여 준다(PC 마다 달라 손으로만): cargo test -p ragsearchbox -- --ignored pdf_app --nocapture
    #[test]
    #[ignore]
    fn pdf_app() {
        let exe = crate::win::assoc_exe(".pdf");
        println!("기본 PDF 앱: {:?}", exe);
        if let Some(e) = exe {
            println!("쪽 인자: {:?}", crate::win::pdf_page_args(&e, r"D:\문서\a.pdf", 5));
        }
    }

    #[test]
    fn url_encoding() {
        assert_eq!(file_url(r"D:\문서 함\a#1.pdf", Some(5)), "file:///D:/%EB%AC%B8%EC%84%9C%20%ED%95%A8/a%231.pdf#page=5");
        assert_eq!(file_url(r"\\srv\share\a.pdf", None), "file://srv/share/a.pdf");
    }
}
