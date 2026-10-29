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
}

impl Browse {
    pub fn new(window: Window, webview: WebView, preview: Option<WebView>) -> Browse {
        Browse { window, webview, preview, visible: false, ready: false, queued: vec![], pv_url: String::new(),
                 pv_rect: (0.0, 0.0, 0.0, 0.0), pv_show: true, pv_pdf: false }
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
        self.window.set_visible(true);
        self.window.set_minimized(false);
        self.window.set_focus();
        self.visible = true;
        crate::win::bring_to_front(self.hwnd());
    }

    pub fn hide(&mut self) {
        self.window.set_visible(false);
        self.visible = false;
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
