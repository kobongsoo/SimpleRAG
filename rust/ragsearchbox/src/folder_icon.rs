//------------------------------------------------------------------
// 인덱싱 폴더 · 드라이브 아이콘 (폴더 아이콘 · 탐색기 모드 설계서 A · §3~§7)
//=> 인덱싱 폴더로 지정한 폴더와 드라이브의 아이콘에 모아이 M 배지를 붙이고, 빼면 원래대로 돌린다.
//   · 폴더: SHGetSetFolderCustomSettings 한 번으로 desktop.ini(UTF-16) · 숨김/시스템 속성 · 폴더 읽기전용 ·
//          탐색기 알림을 함께 처리한다. 원래 값은 desktop.ini 의 [MoAI-S] 절과 상태 파일 두 곳에 남긴다.
//   · 드라이브(C:\ · D:\): desktop.ini 는 "내 PC" 의 드라이브 그림에 듣지 않으므로
//          HKCU\Software\Classes\Applications\Explorer.exe\Drives\<문자>\DefaultIcon 에 쓴다(관리자 불필요).
//   아이콘 파일은 exe 안에 넣어 두고 %LOCALAPPDATA%\RAGSearchBox-rs\icons 로 꺼내 쓴다
//   (폴더 안에 두면 사용자 폴더가 어지러워지고, exe 를 가리키면 옮길 때 깨진다).
//   탐색기 알림 · 네트워크 폴더는 느릴 수 있어 sync 는 자기 스레드에서 부른다.
//------------------------------------------------------------------

use serde_json::{json, Value};
use windows::core::{HSTRING, PCWSTR, PWSTR};
use windows::Win32::Storage::FileSystem::{GetFileAttributesW, SetFileAttributesW, FILE_ATTRIBUTE_NORMAL, FILE_FLAGS_AND_ATTRIBUTES, INVALID_FILE_ATTRIBUTES};
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED};
use windows::Win32::System::WindowsProgramming::{GetPrivateProfileStringW, WritePrivateProfileStringW};
use windows::Win32::UI::Shell::{
    PathUnmakeSystemFolderW, SHChangeNotify, SHGetSetFolderCustomSettings, FCSM_ICONFILE, FCSM_INFOTIP, FCS_FORCEWRITE, FCS_READ,
    SHCNE_ASSOCCHANGED, SHCNE_UPDATEDIR, SHCNE_UPDATEITEM, SHCNF_IDLIST, SHCNF_PATHW, SHFOLDERCUSTOMSETTINGS,
};

const FOLDER_ICO: &[u8] = include_bytes!("../moai-folder.ico");
const DRIVE_ICO: &[u8] = include_bytes!("../moai-drive.ico");
const INFOTIP: &str = "MoAI-S 인덱싱 폴더 — 자연어로 문서와 쪽을 찾을 수 있습니다";
const SECTION: &str = "MoAI-S";
const SHELL: &str = ".ShellClassInfo";
const DRIVES_KEY: &str = r"Software\Classes\Applications\Explorer.exe\Drives";
const ICON_VERSION: &str = "1";

// 앱 데이터 폴더(%LOCALAPPDATA%\RAGSearchBox-rs)
fn data_dir() -> String {
    let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into()));
    format!("{}\\RAGSearchBox-rs", base)
}

fn state_path() -> String {
    format!("{}\\folder_icons.json", data_dir())
}

//------------------------------------------------------------------
// 아이콘 파일 꺼내기 — 없거나 크기가 다르면(그림이 바뀐 새 exe) 다시 쓴다
// -out: (폴더용 경로, 드라이브용 경로) / 쓰기 실패면 Err
//------------------------------------------------------------------
fn ensure_icons() -> Result<(String, String), String> {
    let dir = format!("{}\\icons", data_dir());
    std::fs::create_dir_all(&dir).map_err(|e| format!("아이콘 폴더를 만들지 못했습니다: {}", e))?;
    let mut out = vec![];
    for (name, bytes) in [("moai-folder.ico", FOLDER_ICO), ("moai-drive.ico", DRIVE_ICO)] {
        let p = format!("{}\\{}", dir, name);
        let same = std::fs::metadata(&p).map(|m| m.len() == bytes.len() as u64).unwrap_or(false);
        if !same {
            std::fs::write(&p, bytes).map_err(|e| format!("아이콘을 쓰지 못했습니다: {}", e))?;
        }
        out.push(p);
    }
    Ok((out[0].clone(), out[1].clone()))
}

//------------------------------------------------------------------
// 드라이브 루트인가 — "D:\" · "D:" 모양
// -out: Some('D') / 아니면 None (UNC 공유 루트는 폴더로 다룬다)
//------------------------------------------------------------------
pub fn drive_letter(path: &str) -> Option<char> {
    let p = path.trim_end_matches(['\\', '/']);
    let c: Vec<char> = p.chars().collect();
    if c.len() == 2 && c[1] == ':' && c[0].is_ascii_alphabetic() {
        Some(c[0].to_ascii_uppercase())
    } else {
        None
    }
}

// 상태 파일 — {"entries": [{path, kind, created, orig_icon, orig_tip, attr_set}]}
fn load_state() -> Vec<Value> {
    std::fs::read(state_path()).ok().and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| v["entries"].as_array().cloned()).unwrap_or_default()
}

fn save_state(entries: &[Value]) {
    let _ = std::fs::create_dir_all(data_dir());
    let _ = std::fs::write(state_path(), serde_json::to_vec_pretty(&json!({"entries": entries})).unwrap_or_default());
}

fn same_path(a: &str, b: &str) -> bool {
    a.trim_end_matches(['\\', '/']).eq_ignore_ascii_case(b.trim_end_matches(['\\', '/']))
}

//------------------------------------------------------------------
// 맞춰 보기 (시작할 때 · 폴더를 바꿀 때 · 옵션을 바꿀 때)
//=> 상태 파일과 원하는 목록을 비교해 빠진 것은 붙이고, 남은 것은 되돌린다.
//    1) enabled 가 아니면 원하는 목록 = 없음(모두 되돌림)
//    2) 상태에 있는데 목록에 없으면 restore
//    3) 목록에 있는데 상태에 없으면 apply
// -in: folders = 인덱싱 폴더 목록 / enabled = [UI] FolderIcon
// -out: 실패 안내 목록(성공이면 빈 목록) — 인덱싱은 실패와 상관없이 진행한다
//------------------------------------------------------------------
pub fn sync(folders: &[String], enabled: bool) -> Vec<String> {
    // 셸 함수가 COM 을 쓰므로 이 스레드에서 연다
    let com = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) }.is_ok();
    let want: Vec<String> = if enabled { folders.to_vec() } else { vec![] };
    let mut entries = load_state();
    let mut notes = vec![];
    // 되돌리기
    let mut keep = vec![];
    for e in entries.drain(..) {
        let p = e["path"].as_str().unwrap_or("").to_string();
        if want.iter().any(|w| same_path(w, &p)) {
            keep.push(e);
            continue;
        }
        let r = if e["kind"] == "drive" { restore_drive(&e) } else { restore_folder(&e) };
        match r {
            Ok(()) => crate::info!("icon", "아이콘을 원래대로 돌렸다: {}", p),
            Err(err) => {
                crate::warn!("icon", "아이콘을 원래대로 돌리지 못했다: {} — {}", p, err);
                notes.push(format!("아이콘을 원래대로 돌리지 못함: {} ({})", p, err));
            }
        }
    }
    entries = keep;
    // 붙이기
    if !want.is_empty() {
        match ensure_icons() {
            Err(e) => notes.push(e),
            Ok((folder_ico, drive_ico)) => {
                for f in &want {
                    if entries.iter().any(|e| same_path(e["path"].as_str().unwrap_or(""), f)) {
                        continue;
                    }
                    let r = match drive_letter(f) {
                        Some(d) => apply_drive(f, d, &drive_ico),
                        None => apply_folder(f, &folder_ico),
                    };
                    match r {
                        Ok(e) => {
                            crate::info!("icon", "아이콘을 바꿨다: {}", f);
                            entries.push(e);
                        }
                        Err(err) => {
                            crate::warn!("icon", "아이콘을 바꾸지 못했다: {} — {}", f, err);
                            notes.push(format!("아이콘을 바꾸지 못함: {} ({})", f, err));
                        }
                    }
                }
            }
        }
    }
    save_state(&entries);
    if com {
        unsafe { CoUninitialize() };
    }
    notes
}

// ── 폴더 ──────────────────────────────────────────────────

// desktop.ini 값 읽기(없으면 빈 글)
fn ini_get(ini: &str, sec: &str, key: &str) -> String {
    let mut buf = vec![0u16; 2048];
    let n = unsafe { GetPrivateProfileStringW(&HSTRING::from(sec), &HSTRING::from(key), &HSTRING::from(""), Some(&mut buf), &HSTRING::from(ini)) };
    String::from_utf16_lossy(&buf[..n as usize])
}

// desktop.ini 값 쓰기 — value 가 None 이면 그 키를 지운다
fn ini_set(ini: &str, sec: &str, key: Option<&str>, value: Option<&str>) -> Result<(), String> {
    let k = key.map(HSTRING::from);
    let v = value.map(HSTRING::from);
    unsafe {
        WritePrivateProfileStringW(
            &HSTRING::from(sec),
            k.as_ref().map(|h| PCWSTR(h.as_ptr())).unwrap_or(PCWSTR::null()),
            v.as_ref().map(|h| PCWSTR(h.as_ptr())).unwrap_or(PCWSTR::null()),
            &HSTRING::from(ini),
        )
    }
    .map_err(|e| e.to_string())
}

// 폴더의 지금 아이콘 · 안내글 읽기(셸이 본 값)
fn read_custom(folder: &str) -> (String, String) {
    let mut icon = vec![0u16; 1024];
    let mut tip = vec![0u16; 1024];
    let mut fcs = SHFOLDERCUSTOMSETTINGS {
        dwSize: std::mem::size_of::<SHFOLDERCUSTOMSETTINGS>() as u32,
        dwMask: FCSM_ICONFILE | FCSM_INFOTIP,
        pszIconFile: PWSTR(icon.as_mut_ptr()),
        cchIconFile: icon.len() as u32,
        pszInfoTip: PWSTR(tip.as_mut_ptr()),
        cchInfoTip: tip.len() as u32,
        ..Default::default()
    };
    let ok = unsafe { SHGetSetFolderCustomSettings(&mut fcs, &HSTRING::from(folder), FCS_READ) }.is_ok();
    if !ok {
        return (String::new(), String::new());
    }
    let s = |v: &[u16]| String::from_utf16_lossy(&v[..v.iter().position(|c| *c == 0).unwrap_or(v.len())]);
    (s(&icon), s(&tip))
}

// 탐색기에 "이 폴더 다시 그려라" 알리기 — 폴더와 그 부모(부모 목록 속 아이콘)
fn notify_dir(folder: &str) {
    let w = HSTRING::from(folder);
    let parent = std::path::Path::new(folder).parent().map(|p| HSTRING::from(p.as_os_str()));
    unsafe {
        SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, Some(w.as_ptr() as *const _), None);
        SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, Some(w.as_ptr() as *const _), None);
        if let Some(p) = &parent {
            SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, Some(p.as_ptr() as *const _), None);
        }
    }
}

//------------------------------------------------------------------
// 폴더에 붙이기
//    1) 원래 값 읽기 · desktop.ini 가 있었는지 · 폴더 읽기전용/시스템 속성이 있었는지
//    2) desktop.ini 가 없으면 빈 UTF-16 파일로 먼저 만든다(이후 쓰기가 UTF-16 으로 유지돼 한글이 안 깨짐)
//    3) SHGetSetFolderCustomSettings(FCS_FORCEWRITE) → [MoAI-S] 절에 백업
// -out: 상태 파일 항목 / 실패 문자열
//------------------------------------------------------------------
fn apply_folder(folder: &str, ico: &str) -> Result<Value, String> {
    if !std::path::Path::new(folder).is_dir() {
        return Err("폴더가 없습니다".into());
    }
    let ini = format!("{}\\desktop.ini", folder.trim_end_matches(['\\', '/']));
    let existed = std::path::Path::new(&ini).is_file();
    // 이미 우리가 붙인 폴더(상태 파일만 사라진 경우) — 백업을 그대로 읽어 항목만 되살린다
    if existed && ini_get(&ini, SECTION, "Managed") == "1" {
        return Ok(json!({
            "path": folder, "kind": "folder", "created": ini_get(&ini, SECTION, "Created") == "1",
            "orig_icon": ini_get(&ini, SECTION, "Orig.IconResource"), "orig_tip": ini_get(&ini, SECTION, "Orig.InfoTip"),
            "attr_set": ini_get(&ini, SECTION, "AttrSet") == "1",
        }));
    }
    let (orig_icon, orig_tip) = if existed { (ini_get(&ini, SHELL, "IconResource"), ini_get(&ini, SHELL, "InfoTip")) } else { (String::new(), String::new()) };
    let attrs = unsafe { GetFileAttributesW(&HSTRING::from(folder)) };
    // 읽기전용(1) · 시스템(4) 이 이미 있었으면 되돌릴 때 끄지 않는다
    let attr_set = attrs != INVALID_FILE_ATTRIBUTES && attrs & (0x1 | 0x4) == 0;
    if !existed {
        std::fs::write(&ini, [0xFFu8, 0xFE]).map_err(|e| format!("desktop.ini 를 만들지 못했습니다(권한): {}", e))?;
    }
    let icon_w: Vec<u16> = ico.encode_utf16().chain(Some(0)).collect();
    let tip_w: Vec<u16> = INFOTIP.encode_utf16().chain(Some(0)).collect();
    let mut fcs = SHFOLDERCUSTOMSETTINGS {
        dwSize: std::mem::size_of::<SHFOLDERCUSTOMSETTINGS>() as u32,
        dwMask: FCSM_ICONFILE | FCSM_INFOTIP,
        pszIconFile: PWSTR(icon_w.as_ptr() as *mut u16),
        iIconIndex: 0,
        pszInfoTip: PWSTR(tip_w.as_ptr() as *mut u16),
        ..Default::default()
    };
    if let Err(e) = unsafe { SHGetSetFolderCustomSettings(&mut fcs, &HSTRING::from(folder), FCS_FORCEWRITE) } {
        if !existed {
            let _ = std::fs::remove_file(&ini);
        }
        return Err(format!("폴더 설정을 쓰지 못했습니다: {}", e));
    }
    // 원래 값 백업 — 앱 상태 파일이 사라져도 desktop.ini 만 보고 되돌릴 수 있게
    ini_set(&ini, SECTION, Some("Managed"), Some("1"))?;
    ini_set(&ini, SECTION, Some("Created"), Some(if existed { "0" } else { "1" }))?;
    ini_set(&ini, SECTION, Some("Orig.IconResource"), Some(&orig_icon))?;
    ini_set(&ini, SECTION, Some("Orig.InfoTip"), Some(&orig_tip))?;
    ini_set(&ini, SECTION, Some("AttrSet"), Some(if attr_set { "1" } else { "0" }))?;
    ini_set(&ini, SECTION, Some("IconVersion"), Some(ICON_VERSION))?;
    notify_dir(folder);
    Ok(json!({"path": folder, "kind": "folder", "created": !existed, "orig_icon": orig_icon, "orig_tip": orig_tip, "attr_set": attr_set}))
}

//------------------------------------------------------------------
// 폴더 원래대로
//    1) 지금 아이콘이 우리 것이 아니면(사용자가 그 뒤 바꿨으면) [MoAI-S] 절만 지운다
//    2) 우리가 만든 desktop.ini 면 파일째 지우고, 우리가 켠 폴더 속성도 끈다
//    3) 아니면 원래 IconResource · InfoTip 을 되쓴다(다른 키는 손대지 않음)
//------------------------------------------------------------------
fn restore_folder(e: &Value) -> Result<(), String> {
    let folder = e["path"].as_str().unwrap_or("");
    let ini = format!("{}\\desktop.ini", folder.trim_end_matches(['\\', '/']));
    if !std::path::Path::new(&ini).is_file() {
        return Ok(());
    }
    let (now_icon, _) = read_custom(folder);
    let ours = now_icon.to_lowercase().contains("moai-folder.ico");
    let created = if ini_get(&ini, SECTION, "Managed") == "1" { ini_get(&ini, SECTION, "Created") == "1" } else { e["created"].as_bool().unwrap_or(false) };
    let attr_set = e["attr_set"].as_bool().unwrap_or(false) || ini_get(&ini, SECTION, "AttrSet") == "1";
    if !ours {
        ini_set(&ini, SECTION, None, None)?;
        return Ok(());
    }
    if created {
        // 숨김·시스템 속성이 있으면 지우기가 막힐 수 있어 먼저 푼다
        unsafe {
            let _ = SetFileAttributesW(&HSTRING::from(ini.as_str()), FILE_ATTRIBUTE_NORMAL);
        }
        std::fs::remove_file(&ini).map_err(|e| format!("desktop.ini 를 지우지 못했습니다: {}", e))?;
        if attr_set {
            unsafe {
                let _ = PathUnmakeSystemFolderW(&HSTRING::from(folder));
            }
        }
    } else {
        let oi = ini_get(&ini, SECTION, "Orig.IconResource");
        let ot = ini_get(&ini, SECTION, "Orig.InfoTip");
        let oi = if oi.is_empty() { e["orig_icon"].as_str().unwrap_or("").to_string() } else { oi };
        let ot = if ot.is_empty() { e["orig_tip"].as_str().unwrap_or("").to_string() } else { ot };
        ini_set(&ini, SHELL, Some("IconResource"), if oi.is_empty() { None } else { Some(&oi) })?;
        ini_set(&ini, SHELL, Some("InfoTip"), if ot.is_empty() { None } else { Some(&ot) })?;
        // 셸이 옛 방식 키도 함께 썼을 수 있다
        ini_set(&ini, SHELL, Some("IconFile"), None)?;
        ini_set(&ini, SHELL, Some("IconIndex"), None)?;
        // 셸은 안내글을 .A(ANSI) · .W(UTF-7) 절에도 복사해 둔다(실측) — 남으면 우리 안내글이 계속 보인다
        for sec in [".ShellClassInfo.A", ".ShellClassInfo.W"] {
            ini_set(&ini, sec, Some("InfoTip"), None)?;
        }
        ini_set(&ini, SECTION, None, None)?;
    }
    notify_dir(folder);
    Ok(())
}

// ── 드라이브 ───────────────────────────────────────────────

// 레지스트리 (기본값) 읽기 — 키가 없으면 None, 값이 없으면 Some("")
fn reg_default(key: &str) -> Option<String> {
    use windows::Win32::System::Registry::*;
    unsafe {
        let mut hk = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, &HSTRING::from(key), 0, KEY_READ, &mut hk).is_err() {
            return None;
        }
        let mut buf = vec![0u16; 1024];
        let mut size = (buf.len() * 2) as u32;
        let r = RegQueryValueExW(hk, PCWSTR::null(), None, None, Some(buf.as_mut_ptr() as *mut u8), Some(&mut size));
        let _ = RegCloseKey(hk);
        if r.is_err() {
            return Some(String::new());
        }
        let n = (size as usize / 2).saturating_sub(1);
        Some(String::from_utf16_lossy(&buf[..n.min(buf.len())]))
    }
}

fn reg_set_default(key: &str, value: &str) -> Result<(), String> {
    use windows::Win32::System::Registry::*;
    unsafe {
        let mut hk = HKEY::default();
        RegCreateKeyExW(HKEY_CURRENT_USER, &HSTRING::from(key), 0, PCWSTR::null(), REG_OPTION_NON_VOLATILE, KEY_SET_VALUE, None, &mut hk, None)
            .ok().map_err(|e| e.to_string())?;
        let w: Vec<u16> = value.encode_utf16().chain(Some(0)).collect();
        let bytes = std::slice::from_raw_parts(w.as_ptr() as *const u8, w.len() * 2);
        let r = RegSetValueExW(hk, PCWSTR::null(), 0, REG_SZ, Some(bytes));
        let _ = RegCloseKey(hk);
        r.ok().map_err(|e| e.to_string())
    }
}

// 빈 키 지우기(하위 키가 있으면 실패 — 그대로 둔다)
fn reg_delete(key: &str) {
    use windows::Win32::System::Registry::*;
    unsafe {
        let _ = RegDeleteKeyW(HKEY_CURRENT_USER, &HSTRING::from(key));
    }
}

// 탐색기 알림 — 아이콘 연결이 바뀌었다 + 그 드라이브
fn notify_drive(root: &str) {
    let w = HSTRING::from(root);
    unsafe {
        SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None);
        SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, Some(w.as_ptr() as *const _), None);
    }
}

//------------------------------------------------------------------
// 드라이브에 붙이기 — 원래 (기본값)을 백업하고 우리 아이콘 경로를 쓴다
//------------------------------------------------------------------
fn apply_drive(path: &str, d: char, ico: &str) -> Result<Value, String> {
    let key = format!("{}\\{}\\DefaultIcon", DRIVES_KEY, d);
    let orig = reg_default(&key);
    reg_set_default(&key, &format!("{},0", ico)).map_err(|e| format!("레지스트리에 쓰지 못했습니다: {}", e))?;
    notify_drive(&format!("{}:\\", d));
    Ok(json!({"path": path, "kind": "drive", "letter": d.to_string(), "created": orig.is_none(), "orig_icon": orig.unwrap_or_default()}))
}

//------------------------------------------------------------------
// 드라이브 원래대로 — 우리가 만든 키면 지우고, 원래 값이 있었으면 되쓴다
//   지금 값이 우리 아이콘이 아니면(사용자가 바꿨으면) 건드리지 않는다
//------------------------------------------------------------------
fn restore_drive(e: &Value) -> Result<(), String> {
    let d = e["letter"].as_str().and_then(|s| s.chars().next()).or_else(|| drive_letter(e["path"].as_str().unwrap_or("")));
    let Some(d) = d else { return Ok(()) };
    let base = format!("{}\\{}", DRIVES_KEY, d);
    let key = format!("{}\\DefaultIcon", base);
    let now = reg_default(&key).unwrap_or_default();
    if !now.to_lowercase().contains("moai-drive.ico") {
        return Ok(());
    }
    if e["created"].as_bool().unwrap_or(true) {
        reg_delete(&key);
        reg_delete(&base);
        reg_delete(DRIVES_KEY);
    } else {
        reg_set_default(&key, e["orig_icon"].as_str().unwrap_or("")).map_err(|e| format!("레지스트리를 되돌리지 못했습니다: {}", e))?;
    }
    notify_drive(&format!("{}:\\", d));
    Ok(())
}

#[allow(dead_code)]
fn _unused(_: FILE_FLAGS_AND_ATTRIBUTES) {}

#[cfg(test)]
mod tests {
    use super::*;

    // 실제 셸 · 레지스트리를 건드리므로 손으로만 돌린다:
    //   cargo test -p ragsearchbox -- --ignored apply_restore_roundtrip --nocapture
    //   임시 폴더(RSB_ICON_TEST_DIR) 하나와 HKCU 의 Z 드라이브 키를 바꿨다가 되돌린다(상태 파일은 쓰지 않는다)
    #[test]
    #[ignore]
    fn apply_restore_roundtrip() {
        unsafe {
            let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        }
        let (fico, dico) = ensure_icons().unwrap();
        let dir = std::env::var("RSB_ICON_TEST_DIR").expect("RSB_ICON_TEST_DIR");
        let ini = format!("{}\\desktop.ini", dir);
        // ① desktop.ini 없는 폴더 → 붙이기 → 되돌리면 파일이 사라진다
        let _ = std::fs::remove_file(&ini);
        let e = apply_folder(&dir, &fico).unwrap();
        let (icon, tip) = read_custom(&dir);
        println!("붙인 뒤: icon={} tip={}", icon, tip);
        assert!(icon.to_lowercase().contains("moai-folder.ico"));
        assert_eq!(tip, INFOTIP);
        assert_eq!(ini_get(&ini, SECTION, "Created"), "1");
        let attrs = unsafe { GetFileAttributesW(&HSTRING::from(dir.as_str())) };
        println!("폴더 속성: {:#x}", attrs);
        assert!(attrs & (0x1 | 0x4) != 0);
        restore_folder(&e).unwrap();
        assert!(!std::path::Path::new(&ini).exists());
        let attrs2 = unsafe { GetFileAttributesW(&HSTRING::from(dir.as_str())) };
        println!("되돌린 뒤 속성: {:#x}", attrs2);
        assert!(attrs2 & (0x1 | 0x4) == 0);
        // ② 사용자 desktop.ini 가 있던 폴더 → 원래 키가 되살아난다
        std::fs::write(&ini, [0xFFu8, 0xFE]).unwrap();
        ini_set(&ini, SHELL, Some("InfoTip"), Some("원래 안내")).unwrap();
        ini_set(&ini, SHELL, Some("LocalizedResourceName"), Some("내 폴더")).unwrap();
        let e = apply_folder(&dir, &fico).unwrap();
        assert_eq!(ini_get(&ini, SECTION, "Orig.InfoTip"), "원래 안내");
        restore_folder(&e).unwrap();
        assert_eq!(ini_get(&ini, SHELL, "InfoTip"), "원래 안내");
        assert_eq!(ini_get(&ini, SHELL, "LocalizedResourceName"), "내 폴더");
        assert_eq!(ini_get(&ini, SHELL, "IconResource"), "");
        assert_eq!(ini_get(&ini, SECTION, "Managed"), "");
        assert_eq!(ini_get(&ini, ".ShellClassInfo.W", "InfoTip"), "");
        unsafe {
            let _ = SetFileAttributesW(&HSTRING::from(ini.as_str()), FILE_ATTRIBUTE_NORMAL);
            let _ = PathUnmakeSystemFolderW(&HSTRING::from(dir.as_str()));
        }
        let _ = std::fs::remove_file(&ini);
        // ③ 드라이브 키(Z) — 없던 키를 만들었다가 지운다
        let key = format!("{}\\Z\\DefaultIcon", DRIVES_KEY);
        assert!(reg_default(&key).is_none(), "Z 키가 이미 있어 시험하지 않는다");
        let e = apply_drive("Z:\\", 'Z', &dico).unwrap();
        println!("드라이브 키: {:?}", reg_default(&key));
        assert!(reg_default(&key).unwrap().contains("moai-drive.ico"));
        restore_drive(&e).unwrap();
        assert!(reg_default(&key).is_none());
    }

    #[test]
    fn drive_root_detect() {
        assert_eq!(drive_letter("D:\\"), Some('D'));
        assert_eq!(drive_letter("c:"), Some('C'));
        assert_eq!(drive_letter("D:\\문서"), None);
        assert_eq!(drive_letter("\\\\server\\share"), None);
    }
}
