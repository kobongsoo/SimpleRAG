//------------------------------------------------------------------
// 로그 (Python RAGSearchBox/log.py)
//=> %LOCALAPPDATA%\MoAISearch\log 아래 날짜별 두 파일에 남긴다(자정이 지나면 새 파일).
//    · MoAISearch_2026-11-02.log        — 프로그램 동작(설정 단계 이상)
//    · MoAISearch-worker_2026-11-02.log — 워커(simplerag-rs)가 stderr 로 낸 줄
//   대화 기록 · WebView · 폴더 아이콘은 로그와 따로 app_dir()(%LOCALAPPDATA%\RAGSearchBox-rs)에 둔다(로그 보기 설계서 §6).
//   하루치가 5MB 를 넘으면 .1 .2 .3 으로 돌려 쓴다. DIAG 는 INFO 보다 낮은 자세한 단계다.
//------------------------------------------------------------------

use std::io::Write;
use std::sync::{Mutex, OnceLock};

const MAX_BYTES: u64 = 5 * 1024 * 1024;
const BACKUPS: usize = 3;

pub const DIAG: u8 = 5;
pub const INFO: u8 = 20;
pub const WARNING: u8 = 30;
pub const ERROR: u8 = 40;

static LEVEL: OnceLock<u8> = OnceLock::new();
static LOCK: Mutex<()> = Mutex::new(());

pub const MAIN_BASE: &str = "MoAISearch";
pub const WORKER_BASE: &str = "MoAISearch-worker";

// %LOCALAPPDATA%(없으면 사용자 폴더)
fn local_base() -> String {
    std::env::var("LOCALAPPDATA").unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into()))
}

//------------------------------------------------------------------
// 앱 데이터 폴더 — 대화 기록 · WebView · 폴더 아이콘(%LOCALAPPDATA%\RAGSearchBox-rs)
//=> 예전엔 로그 폴더의 부모를 썼다. 로그 폴더만 MoAISearch 로 옮기면서 이 둘이 따라 옮겨져
//   대화 기록이 사라진 것처럼 보이지 않게 자리를 따로 못 박는다.
//------------------------------------------------------------------
pub fn app_dir() -> String {
    let d = std::path::Path::new(&local_base()).join("RAGSearchBox-rs");
    let _ = std::fs::create_dir_all(&d);
    d.to_string_lossy().into_owned()
}

//------------------------------------------------------------------
// 로그 폴더(%LOCALAPPDATA%\MoAISearch\log) — 없으면 만든다
//------------------------------------------------------------------
pub fn log_dir() -> String {
    let d = std::path::Path::new(&local_base()).join("MoAISearch").join("log");
    let _ = std::fs::create_dir_all(&d);
    d.to_string_lossy().into_owned()
}

// 오늘 파일 이름 — 설정 > 로그 판에 보인다
pub fn today_names() -> (String, String) {
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    (dated_name(MAIN_BASE, &today), dated_name(WORKER_BASE, &today))
}

// 지금 기록 단계 이름
pub fn level_name() -> &'static str {
    match *LEVEL.get().unwrap_or(&INFO) {
        DIAG => "DIAG",
        10 => "DEBUG",
        WARNING => "WARNING",
        ERROR => "ERROR",
        _ => "INFO",
    }
}

//------------------------------------------------------------------
// 단계 정하기(INFO · DIAG · DEBUG · WARNING · ERROR)
//------------------------------------------------------------------
pub fn setup(level: &str) {
    let lv = match level.to_uppercase().as_str() {
        "DIAG" => DIAG,
        "DEBUG" => 10,
        "WARNING" => WARNING,
        "ERROR" => ERROR,
        _ => INFO,
    };
    let _ = LEVEL.set(lv);
}

pub fn enabled(lv: u8) -> bool {
    lv >= *LEVEL.get().unwrap_or(&INFO)
}

// 날짜가 붙은 파일 이름 — ("MoAISearch", "2026-11-02") → "MoAISearch_2026-11-02.log"
pub fn dated_name(base: &str, date: &str) -> String {
    format!("{}_{}.log", base, date)
}

// 오늘 파일에 한 줄 덧붙이기(넘치면 돌려 쓰기) — 쓸 때마다 날짜를 보므로 자정이 지나면 저절로 새 파일이 된다
fn append(base: &str, line: &str) {
    let _g = LOCK.lock().unwrap_or_else(|p| p.into_inner());
    let dir = log_dir();
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    let path = std::path::Path::new(&dir).join(dated_name(base, &today));
    if std::fs::metadata(&path).map(|m| m.len() > MAX_BYTES).unwrap_or(false) {
        for i in (1..BACKUPS).rev() {
            let _ = std::fs::rename(format!("{}.{}", path.display(), i), format!("{}.{}", path.display(), i + 1));
        }
        let _ = std::fs::rename(&path, format!("{}.1", path.display()));
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{}", line);
    }
}

//------------------------------------------------------------------
// 프로그램 로그 한 줄 — "시각 단계 이름 내용"
//------------------------------------------------------------------
pub fn write(lv: u8, name: &str, msg: &str) {
    if !enabled(lv) {
        return;
    }
    let lname = match lv {
        DIAG => "DIAG ",
        10 => "DEBUG",
        INFO => "INFO ",
        WARNING => "WARNI",
        _ => "ERROR",
    };
    let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
    append(MAIN_BASE, &format!("{} {} rsb.{} {}", now, lname, name, msg));
}

// 워커 stderr 한 줄
pub fn worker(msg: &str) {
    let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
    append(WORKER_BASE, &format!("{} INFO  rsbworker {}", now, msg));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_has_date() {
        assert_eq!(dated_name(MAIN_BASE, "2026-11-02"), "MoAISearch_2026-11-02.log");
        assert_eq!(dated_name(WORKER_BASE, "2026-11-03"), "MoAISearch-worker_2026-11-03.log");
    }
}

#[macro_export]
macro_rules! info { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::INFO, $name, &format!($($a)*)) } }
#[macro_export]
macro_rules! warn { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::WARNING, $name, &format!($($a)*)) } }
#[macro_export]
macro_rules! diag { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::DIAG, $name, &format!($($a)*)) } }
#[macro_export]
macro_rules! error { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::ERROR, $name, &format!($($a)*)) } }
