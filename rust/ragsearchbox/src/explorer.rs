//------------------------------------------------------------------
// 탐색기 창이 보고 있는 폴더 알아내기 + 검색창에 쓰기 (Python explorer.py · searchbox.py · evidence_list.py)
//=> Shell.Application 의 창 목록(IShellWindows)에서 그 탐색기 창의 탭들(주소·이름)을 받고,
//   탭 제목으로 지금 앞에 보이는 탭을 고른다. 탐색기 창마다 COM 을 쓰므로 감시 스레드에서만 부른다.
//   "탐색기에서 보기" 는 UI Automation 으로 탐색기 검색창을 찾아 근거 파일 이름 질의를 써 넣는다.
//------------------------------------------------------------------

use std::collections::HashMap;

use windows::core::{Interface, BSTR, HSTRING};
use windows::Win32::System::Com::{CoCreateInstance, IDispatch, CLSCTX_ALL, CLSCTX_INPROC_SERVER};
use windows::core::VARIANT;
use windows::Win32::UI::Accessibility::*;
use windows::Win32::UI::Shell::{IShellWindows, IWebBrowser2, ShellWindows};

use crate::scope;
use crate::win;

//------------------------------------------------------------------
// 탭 하나 — 이름·실제 경로(파일 시스템 폴더일 때만, 주소 file:/// 에서 푼다)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Entry {
    pub name: String,
    pub path: Option<String>,
}

//------------------------------------------------------------------
// file:/// 주소 → 경로 (퍼센트 인코딩을 푼다)
//------------------------------------------------------------------
pub fn url_to_path(url: &str) -> Option<String> {
    let low = url.to_lowercase();
    let dec = |s: &str| percent_encoding::percent_decode_str(s).decode_utf8_lossy().replace('/', "\\");
    if low.starts_with("file:///") {
        return Some(dec(&url[8..]));
    }
    if low.starts_with("file://") {
        return Some(format!("\\\\{}", dec(&url[7..])));
    }
    None
}

// 탭 종류 — 진짜 폴더 / 검색 결과 / 가상 폴더
fn classify(e: &Entry, search_text: &str) -> &'static str {
    if e.path.is_some() {
        return "folder";
    }
    let (name, text) = (e.name.trim(), search_text.trim());
    if !text.is_empty() && !name.is_empty() && name == text {
        return "search";
    }
    "virtual"
}

// 탭 제목이 이 탭과 맞나
fn title_matches(e: &Entry, kind: &str, title: &str) -> bool {
    let title = title.trim();
    if title.is_empty() {
        return false;
    }
    if kind == "folder" {
        let p = e.path.clone().unwrap_or_default();
        if scope::norm(&p) == scope::norm(title) {
            return true;
        }
        let base = p.trim_end_matches(['\\', '/']).rsplit(['\\', '/']).next().unwrap_or("").to_lowercase();
        return base == title.to_lowercase();
    }
    let name = e.name.trim();
    if name.is_empty() {
        return false;
    }
    if kind == "search" {
        return title == name || title.starts_with(&format!("{} ", name));
    }
    title == name
}

// 탭 → 실제 폴더(검색 결과 탭이면 기억해 둔 폴더)
fn resolve(e: &Entry, kind: &str, title: &str, remembered: Option<&String>) -> Option<String> {
    if kind == "folder" {
        return e.path.clone();
    }
    if kind == "search" {
        if let Some(r) = remembered {
            let base = scope::norm(r).rsplit('\\').next().unwrap_or("").to_string();
            if !base.is_empty() && title.to_lowercase().contains(&base) {
                return Some(r.clone());
            }
        }
    }
    None
}

//------------------------------------------------------------------
// 앞에 보이는 탭 고르기 — (후보 경로들, 판단 이유)
//------------------------------------------------------------------
pub fn pick_active(entries: &[Entry], titles: &[String], search_text: &str, remembered: Option<&String>) -> (Vec<Option<String>>, String) {
    if entries.is_empty() {
        return (vec![], "탐색기 목록에 없음".into());
    }
    let title = titles.first().cloned().unwrap_or_default();
    if entries.len() == 1 {
        let k = classify(&entries[0], search_text);
        return (vec![resolve(&entries[0], k, &title, remembered)], format!("탭 1개 ({})", k));
    }
    let matched: Vec<(&Entry, &str)> = entries.iter().map(|e| (e, classify(e, search_text))).filter(|(e, k)| title_matches(e, k, &title)).collect();
    if matched.len() == 1 {
        let (e, k) = matched[0];
        return (vec![resolve(e, k, &title, remembered)], format!("활성 탭 일치 ({})", k));
    }
    let pool: Vec<(&Entry, &str)> = if !matched.is_empty() { matched } else { entries.iter().map(|e| (e, classify(e, search_text))).collect() };
    let paths: Vec<Option<String>> = pool.iter().map(|(e, k)| resolve(e, k, &title, remembered)).collect();
    let n = paths.len();
    (paths, format!("활성 탭을 가리지 못함 — 후보 {}개", n))
}

//------------------------------------------------------------------
// 탐색기 위치 찾개 — COM 객체를 붙들고 창마다 마지막 폴더를 기억한다
//------------------------------------------------------------------
pub struct Locator {
    shell: Option<IShellWindows>,
    pub last_folder: HashMap<isize, String>,
}

impl Locator {
    pub fn new() -> Locator {
        Locator { shell: None, last_folder: HashMap::new() }
    }

    fn shell(&mut self) -> Option<&IShellWindows> {
        if self.shell.is_none() {
            self.shell = unsafe { CoCreateInstance::<_, IShellWindows>(&ShellWindows, None, CLSCTX_ALL) }.ok();
            if self.shell.is_none() {
                crate::warn!("explorer", "Shell.Application 창 목록을 만들지 못했다");
            }
        }
        self.shell.as_ref()
    }

    //------------------------------------------------------------------
    // 그 탐색기 창의 탭들
    //------------------------------------------------------------------
    pub fn entries(&mut self, hwnd: isize) -> Vec<Entry> {
        let mut out = vec![];
        let sw = match self.shell() {
            Some(s) => s.clone(),
            None => return out,
        };
        unsafe {
            let n = sw.Count().unwrap_or(0);
            for i in 0..n {
                let d: IDispatch = match sw.Item(&VARIANT::from(i)) {
                    Ok(d) => d,
                    Err(_) => continue,
                };
                let wb: IWebBrowser2 = match d.cast() {
                    Ok(w) => w,
                    Err(_) => continue,
                };
                let wh = wb.HWND().map(|x| x.0 as isize).unwrap_or(0);
                if wh != hwnd {
                    continue;
                }
                let url = wb.LocationURL().map(|b| b.to_string()).unwrap_or_default();
                let name = wb.LocationName().map(|b| b.to_string()).unwrap_or_default();
                let path = url_to_path(&url);
                out.push(Entry { name, path });
            }
        }
        out
    }

    //------------------------------------------------------------------
    // 그 창이 보고 있는 폴더 — (후보, 이유)
    //------------------------------------------------------------------
    pub fn folder_of(&mut self, hwnd: isize, search_text: &str) -> (Vec<Option<String>>, String) {
        let ents = self.entries(hwnd);
        let titles = win::tab_titles(hwnd);
        let remembered = self.last_folder.get(&hwnd).cloned();
        let (paths, why) = pick_active(&ents, &titles, search_text, remembered.as_ref());
        if paths.len() == 1 {
            if let Some(p) = &paths[0] {
                if ents.iter().any(|e| e.path.is_some()) {
                    self.last_folder.insert(hwnd, p.clone());
                }
            }
        }
        (paths, why)
    }

    // 닫힌 창은 잊는다
    pub fn forget_closed(&mut self) {
        self.last_folder.retain(|h, _| win::is_window(*h));
    }
}

// ── "탐색기에서 보기" — 검색창에 근거 파일 이름 질의 쓰기 ─────

//------------------------------------------------------------------
// 파일 이름 따옴표로 감싸기 — 이름 안의 따옴표는 가장 긴 조각만 남긴다
//------------------------------------------------------------------
fn quote_name(name: &str) -> Option<String> {
    let n = name.trim().trim_matches('"');
    let base = n.rsplit(['\\', '/']).next().unwrap_or("").to_string();
    if base.is_empty() {
        return None;
    }
    let base = if base.contains('"') {
        base.split('"').filter(|p| !p.trim().is_empty()).max_by_key(|p| p.len())?.trim().to_string()
    } else {
        base
    };
    if base.is_empty() {
        return None;
    }
    Some(format!("\"{}\"", base))
}

//------------------------------------------------------------------
// 질의 만들기 — "a.pdf" OR "b.docx" (최대 10개, 중복 없음)
//------------------------------------------------------------------
pub fn build_query(names: &[String]) -> Option<String> {
    let mut out: Vec<String> = vec![];
    for n in names {
        if let Some(q) = quote_name(n) {
            if !out.contains(&q) {
                out.push(q);
            }
            if out.len() >= 10 {
                break;
            }
        }
    }
    if out.is_empty() { None } else { Some(out.join(" OR ")) }
}

//------------------------------------------------------------------
// 탐색기 검색창에 글 쓰기(포커스를 뺏지 않는다) — Ok(쓴 글) / Err(사유)
//=> Win11 은 부모 요소(FileExplorerSearchBox) 아래 Edit, 그 아래에 LegacyIAccessible.SetValue
//------------------------------------------------------------------
pub fn show_evidence(hwnd: isize, names: &[String]) -> Result<String, String> {
    let query = build_query(names).ok_or("근거 파일 이름이 없습니다")?;
    if !win::is_window(hwnd) {
        return Err("창이 닫혔습니다".into());
    }
    unsafe {
        if windows::Win32::UI::WindowsAndMessaging::IsIconic(win::h(hwnd)).as_bool() {
            return Err("창이 최소화되어 있습니다".into());
        }
        let uia: IUIAutomation = CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER).map_err(|_| "UI Automation 을 시작하지 못했습니다".to_string())?;
        let root = uia.ElementFromHandle(win::h(hwnd)).map_err(|_| "검색창을 찾지 못했습니다".to_string())?;
        let cond = uia.CreatePropertyCondition(UIA_AutomationIdPropertyId, &VARIANT::from(BSTR::from("FileExplorerSearchBox"))).map_err(|e| e.to_string())?;
        let edit = match root.FindFirst(TreeScope_Descendants, &cond) {
            Ok(b) => {
                let c2 = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, &VARIANT::from(UIA_EditControlTypeId.0)).map_err(|e| e.to_string())?;
                b.FindFirst(TreeScope_Descendants, &c2).ok()
            }
            Err(_) => None,
        };
        // Win10 은 Edit 자신의 AutomationId 가 SearchEditBox 다
        let edit = match edit {
            Some(e) => e,
            None => {
                let c3 = uia.CreatePropertyCondition(UIA_AutomationIdPropertyId, &VARIANT::from(BSTR::from("SearchEditBox"))).map_err(|e| e.to_string())?;
                root.FindFirst(TreeScope_Descendants, &c3).map_err(|_| "검색창을 찾지 못했습니다".to_string())?
            }
        };
        let pat: IUIAutomationLegacyIAccessiblePattern = edit.GetCurrentPatternAs(UIA_LegacyIAccessiblePatternId).map_err(|_| "검색창에 쓰지 못했습니다".to_string())?;
        pat.SetValue(&HSTRING::from(query.as_str())).map_err(|_| "검색창에 쓰지 못했습니다".to_string())?;
    }
    crate::info!("evidence", "근거 목록 표시: {}", query.chars().take(80).collect::<String>());
    Ok(query)
}
