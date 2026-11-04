//------------------------------------------------------------------
// 검색 폴더로 넣을 수 있는가 — 폴더 추가 때 거르기
//=> 드라이브 전체 · 네트워크 · 클라우드 동기화 폴더 · 시스템 폴더는 넣지 않는다.
//    1) 드라이브 전체(C:\) — 시스템 · 프로그램 파일까지 수십만 개를 훑게 된다
//    2) 네트워크(\\서버\공유, 연결한 네트워크 드라이브) — 느리고 끊기면 문서가 한꺼번에 사라진 것으로 보인다
//    3) 클라우드(OneDrive · Google Drive · Dropbox · iCloud) — 열 때마다 내려받는 빈 파일(자리 표시)이라
//       인덱싱이 전체를 내려받게 만들고, 동기화 중 바뀜이 끝없이 다시 인덱싱을 부른다
//    4) 시스템 · 앱 데이터 · 숨김 폴더 안 — 인덱서가 들어가지 않는 곳(simplerag indexer.rs 의 default_skip_dir 와 같게)
//------------------------------------------------------------------
use windows::core::PCWSTR;
use windows::Win32::Storage::FileSystem::{FindClose, FindFirstFileW, GetDriveTypeW, GetVolumeInformationW, WIN32_FIND_DATAW};

// 드라이브 바로 아래 시스템 폴더 · 어디서든 거르는 이름 — 인덱서와 같은 목록
const SYSTEM_TOP_DIRS: &[&str] = &["windows", "windows.old", "program files", "program files (x86)", "programdata", "recovery",
    "perflogs", "$winreagent", "$sysreset", "$windows.~bt", "$windows.~ws", "config.msi", "msocache", "onedrivetemp"];
const SKIP_DIR_NAMES: &[&str] = &["$recycle.bin", "system volume information", "appdata", "node_modules", ".git", ".svn", ".hg",
    ".venv", "venv", "__pycache__", "site-packages", ".idea", ".vscode", ".cache", ".tox", ".mypy_cache", ".pytest_cache", ".gradle"];
// 클라우드 동기화 폴더 이름(폴더 이름 하나와 같거나 "OneDrive - 회사" 처럼 시작)
const CLOUD_NAMES: &[&str] = &["onedrive", "google drive", "googledrive", "my drive", "내 드라이브", "dropbox", "icloud drive", "iclouddrive"];

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

//------------------------------------------------------------------
// 넣을 수 없는 까닭 — 넣어도 되면 None
// -in: path = 고른 폴더(절대 경로)
// -out: Some(안내 글) / None
//------------------------------------------------------------------
pub fn refusal(path: &str) -> Option<String> {
    let p = path.replace('/', "\\");
    let trimmed = p.trim_end_matches('\\');
    // 네트워크 경로(\\서버\공유 · \\?\UNC\…)
    if p.starts_with("\\\\") {
        return Some("네트워크 폴더(\\\\서버\\공유)는 검색 폴더로 넣을 수 없습니다 — 이 PC 의 폴더를 고르세요".into());
    }
    let b = trimmed.as_bytes();
    if b.len() < 2 || b[1] != b':' {
        return Some(format!("폴더 경로를 알 수 없습니다: {}", path));
    }
    if trimmed.len() <= 2 {
        return Some(format!("드라이브 전체({}\\)는 검색 폴더로 넣을 수 없습니다 — 그 안의 폴더를 고르세요", &trimmed[..2]));
    }
    let drive = format!("{}\\", &trimmed[..2]);
    let dw = wide(&drive);
    // DRIVE_REMOTE(4) — 연결한 네트워크 드라이브
    if unsafe { GetDriveTypeW(PCWSTR(dw.as_ptr())) } == 4 {
        return Some(format!("네트워크 드라이브({})의 폴더는 검색 폴더로 넣을 수 없습니다 — 이 PC 의 폴더를 고르세요", drive));
    }
    // Google Drive 데스크톱은 가상 드라이브(이름 · 파일 시스템에 Google)로 붙는다
    let (mut label, mut fs) = ([0u16; 261], [0u16; 261]);
    if unsafe { GetVolumeInformationW(PCWSTR(dw.as_ptr()), Some(&mut label), None, None, None, Some(&mut fs)) }.is_ok() {
        let s = |v: &[u16]| String::from_utf16_lossy(&v[..v.iter().position(|&c| c == 0).unwrap_or(v.len())]).to_lowercase();
        if s(&label).contains("google") || s(&fs).contains("google") {
            return Some(format!("클라우드 드라이브({} Google Drive)는 검색 폴더로 넣을 수 없습니다", drive));
        }
    }
    let low = trimmed.to_lowercase();
    // OneDrive 가 알려 준 동기화 폴더 안
    for k in ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"] {
        if let Ok(v) = std::env::var(k) {
            let v = v.replace('/', "\\").trim_end_matches('\\').to_lowercase();
            if !v.is_empty() && (low == v || low.starts_with(&format!("{}\\", v))) {
                return Some("OneDrive 동기화 폴더는 검색 폴더로 넣을 수 없습니다".into());
            }
        }
    }
    // 위쪽 폴더를 하나씩 — 클라우드 이름 · 시스템 폴더 · 숨김 · 클라우드 자리 표시 폴더
    let parts: Vec<&str> = trimmed.split('\\').collect();
    for i in 1..parts.len() {
        let dir = parts[..=i].join("\\");
        let name = parts[i].to_lowercase();
        if CLOUD_NAMES.iter().any(|c| name == *c || name.starts_with(&format!("{} - ", c))) {
            return Some(format!("클라우드 동기화 폴더({})는 검색 폴더로 넣을 수 없습니다", parts[i]));
        }
        if (i == 1 && SYSTEM_TOP_DIRS.contains(&name.as_str())) || SKIP_DIR_NAMES.contains(&name.as_str()) {
            return Some(format!("시스템 · 프로그램 · 앱 데이터 · 개발 도구 폴더({})는 검색 폴더로 넣을 수 없습니다", dir));
        }
        let dwd = wide(&dir);
        let mut fd = WIN32_FIND_DATAW::default();
        if let Ok(h) = unsafe { FindFirstFileW(PCWSTR(dwd.as_ptr()), &mut fd) } {
            let _ = unsafe { FindClose(h) };
            // 숨김(0x2)
            if fd.dwFileAttributes & 0x2 != 0 {
                return Some(format!("숨김 폴더({})는 검색 폴더로 넣을 수 없습니다", dir));
            }
            // 클라우드 자리 표시(재분석 지점 0x400 + IO_REPARSE_TAG_CLOUD 계열) — OneDrive · iCloud 등
            if fd.dwFileAttributes & 0x400 != 0 && fd.dwReserved0 & 0xFFFF_0FFF == 0x9000_001A {
                return Some(format!("클라우드 동기화 폴더({})는 검색 폴더로 넣을 수 없습니다", dir));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::refusal;

    // 막아야 할 곳과 넣어도 되는 곳
    #[test]
    fn refuse_and_allow() {
        for p in [r"C:\", r"D:\", r"C:\Windows\System32", r"C:\Program Files\Common Files", r"C:\ProgramData",
                  r"\\server\share\docs", r"D:\Project\SimpleRAG\.venv\Lib", r"C:\Users\me\OneDrive - 회사\문서",
                  r"C:\Users\me\Dropbox", r"C:\$Recycle.Bin"] {
            assert!(refusal(p).is_some(), "막혀야 함: {}", p);
            println!("{} → {}", p, refusal(p).unwrap());
        }
        let home = std::env::var("USERPROFILE").unwrap();
        for p in [format!(r"{}\Desktop", home), r"D:\Project\SimpleRAG\doc".to_string()] {
            assert!(refusal(&p).is_none(), "넣어져야 함: {} — {:?}", p, refusal(&p));
        }
        if let Ok(od) = std::env::var("OneDrive") {
            println!("OneDrive {} → {:?}", od, refusal(&od));
        }
        println!("AppData → {:?}", refusal(&format!(r"{}\AppData\Local\Programs", home)));
    }
}
