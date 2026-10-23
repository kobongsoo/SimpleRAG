//------------------------------------------------------------------
// 로그 (Python RAGSearchBox/log.py)
//=> %LOCALAPPDATA%\RAGSearchBox-rs\log 아래 두 파일에 남긴다.
//    · RAGSearchBox.log — 프로그램 동작(설정 단계 이상)
//    · worker.log       — 워커(simplerag-rs)가 stderr 로 낸 줄
//   파일이 5MB 를 넘으면 .1 .2 .3 으로 돌려 쓴다. DIAG 는 INFO 보다 낮은 자세한 단계다.
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

//------------------------------------------------------------------
// 로그 폴더 — 없으면 만든다
//------------------------------------------------------------------
pub fn log_dir() -> String {
    let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into()));
    let d = std::path::Path::new(&base).join("RAGSearchBox-rs").join("log");
    let _ = std::fs::create_dir_all(&d);
    d.to_string_lossy().into_owned()
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

// 파일 하나에 한 줄 덧붙이기(넘치면 돌려 쓰기)
fn append(file: &str, line: &str) {
    let _g = LOCK.lock().unwrap_or_else(|p| p.into_inner());
    let dir = log_dir();
    let path = std::path::Path::new(&dir).join(file);
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
    append("RAGSearchBox.log", &format!("{} {} rsb.{} {}", now, lname, name, msg));
}

// 워커 stderr 한 줄
pub fn worker(msg: &str) {
    let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
    append("worker.log", &format!("{} INFO  rsbworker {}", now, msg));
}

#[macro_export]
macro_rules! info { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::INFO, $name, &format!($($a)*)) } }
#[macro_export]
macro_rules! warn { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::WARNING, $name, &format!($($a)*)) } }
#[macro_export]
macro_rules! diag { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::DIAG, $name, &format!($($a)*)) } }
#[macro_export]
macro_rules! error { ($name:expr, $($a:tt)*) => { $crate::log::write($crate::log::ERROR, $name, &format!($($a)*)) } }
