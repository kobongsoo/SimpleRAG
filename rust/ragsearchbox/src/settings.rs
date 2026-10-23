//------------------------------------------------------------------
// RAGSearchBox.ini 읽기·쓰기 (Python RAGSearchBox/settings.py)
//=> 없는 항목은 기본값, 범위 밖 값은 기본값으로 되돌리고 경고 목록에 적는다(트레이·로그로 알린다).
//   INI 는 exe 옆 RAGSearchBox.ini. 값을 바꿀 때는 그 줄만 고쳐 주석·정렬을 지킨다.
//------------------------------------------------------------------

use std::collections::HashMap;

//------------------------------------------------------------------
// 설정 묶음 (필드 이름은 Python 판과 같다)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Settings {
    pub simplerag_exe: String,
    pub no_stream: bool,
    pub relevance_min: String,
    pub start_mode: String,
    pub idle_unload_min: i64,
    pub start_timeout_s: i64,
    pub answer_timeout_s: i64,
    pub restart_max: i64,
    pub monitor_mode: i64,
    pub poll_ms: i64,
    pub input_recent_ms: i64,
    pub prefix: String,
    pub min_chars: i64,
    pub dedup_sec: i64,
    pub scope_folders: Vec<String>,
    pub scope_recheck_min: i64,
    pub panel_enabled: bool,
    pub shrink_explorer: bool,
    pub folder_poll_ms: i64,
    pub panel_search_scope: String,
    pub searchbox_trigger: bool,
    pub dock: String,
    pub win_width: i64,
    pub win_max_height: i64,
    pub font_size: i64,
    pub autoindex_enabled: bool,
    pub autoindex_quiet_s: i64,
    pub autoindex_rescan_min: i64,
    pub autoindex_ask_above: i64,
    pub autoindex_bm25_idle_s: i64,
    pub log_level: String,
    pub warnings: Vec<String>,
    pub ini_path: String,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            simplerag_exe: String::new(),
            no_stream: false,
            relevance_min: "-6".into(),
            start_mode: "boot".into(),
            idle_unload_min: 60,
            start_timeout_s: 180,
            answer_timeout_s: 60,
            restart_max: 3,
            monitor_mode: 0,
            poll_ms: 100,
            input_recent_ms: 1500,
            prefix: "?".into(),
            min_chars: 2,
            dedup_sec: 3,
            scope_folders: vec![],
            scope_recheck_min: 5,
            panel_enabled: true,
            shrink_explorer: true,
            folder_poll_ms: 500,
            panel_search_scope: "folder".into(),
            searchbox_trigger: false,
            dock: "off".into(),
            win_width: 460,
            win_max_height: 560,
            font_size: 10,
            autoindex_enabled: true,
            autoindex_quiet_s: 5,
            autoindex_rescan_min: 10,
            autoindex_ask_above: 50,
            autoindex_bm25_idle_s: 10,
            log_level: "INFO".into(),
            warnings: vec![],
            ini_path: String::new(),
        }
    }
}

//------------------------------------------------------------------
// 프로그램 폴더(exe 옆)
//------------------------------------------------------------------
pub fn app_dir() -> String {
    std::env::current_exe().ok().and_then(|p| p.parent().map(|d| d.to_string_lossy().into_owned())).unwrap_or_else(|| ".".into())
}

pub const INI_NAME: &str = "RAGSearchBox.ini";

// INI 를 {섹션(소문자): {키(소문자): 값}} 으로(configparser 처럼 주석 줄은 버린다)
fn parse_ini(text: &str) -> HashMap<String, HashMap<String, String>> {
    let mut out: HashMap<String, HashMap<String, String>> = HashMap::new();
    let mut sec = String::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with(';') || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            sec = line[1..line.len() - 1].trim().to_lowercase();
            continue;
        }
        let (k, v) = match line.split_once('=').or_else(|| line.split_once(':')) {
            Some(x) => x,
            None => continue,
        };
        out.entry(sec.clone()).or_default().insert(k.trim().to_lowercase(), v.trim().to_string());
    }
    out
}

// 값 꺼내기 도우미
struct Ini {
    m: HashMap<String, HashMap<String, String>>,
}
impl Ini {
    fn get(&self, sec: &str, key: &str) -> Option<String> {
        self.m.get(&sec.to_lowercase()).and_then(|s| s.get(&key.to_lowercase())).cloned()
    }
    fn s(&self, sec: &str, key: &str) -> String {
        self.get(sec, key).unwrap_or_default().trim().to_string()
    }
    fn int(&self, sec: &str, key: &str, default: i64, lo: i64, hi: i64, w: &mut Vec<String>) -> i64 {
        let raw = self.s(sec, key);
        if raw.is_empty() {
            return default;
        }
        match raw.parse::<i64>() {
            Err(_) => {
                w.push(format!("[{}] {} = {} 는 숫자가 아니라 기본값 {} 을 씁니다", sec, key, raw, default));
                default
            }
            Ok(v) if v < lo || v > hi => {
                w.push(format!("[{}] {} = {} 는 허용 범위({}~{}) 밖이라 기본값 {} 을 씁니다", sec, key, v, lo, hi, default));
                default
            }
            Ok(v) => v,
        }
    }
    fn boolean(&self, sec: &str, key: &str, default: bool, w: &mut Vec<String>) -> bool {
        let raw = self.s(sec, key).to_lowercase();
        if raw.is_empty() {
            return default;
        }
        match raw.as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => {
                w.push(format!("[{}] {} = {} 를 알 수 없어 기본값 {} 을 씁니다", sec, key, raw, if default { "True" } else { "False" }));
                default
            }
        }
    }
}

//------------------------------------------------------------------
// 설정 읽기 (진입점)
//------------------------------------------------------------------
pub fn load(path: Option<&str>) -> Settings {
    let mut s = Settings { ini_path: path.map(|p| p.to_string()).unwrap_or_else(|| format!("{}\\{}", app_dir(), INI_NAME)), ..Default::default() };
    let raw = match std::fs::read(&s.ini_path) {
        Ok(b) => b,
        Err(_) => {
            s.warnings.push(format!("설정 파일이 없어 기본값으로 동작합니다: {}", s.ini_path));
            return s;
        }
    };
    let text = String::from_utf8_lossy(&raw).trim_start_matches('\u{feff}').to_string();
    let ini = Ini { m: parse_ini(&text) };
    let mut w = vec![];
    s.simplerag_exe = ini.s("SimpleRAG", "SimpleRagExe");
    s.no_stream = ini.boolean("SimpleRAG", "NoStream", false, &mut w);
    let rel = ini.s("SimpleRAG", "RelevanceMin").to_lowercase();
    if rel == "off" || rel == "none" {
        s.relevance_min = "off".into();
    } else if !rel.is_empty() {
        match rel.parse::<f64>() {
            Ok(v) if (-20.0..=20.0).contains(&v) => s.relevance_min = format!("{:?}", v),
            _ => w.push(format!("[SimpleRAG] RelevanceMin = {} 는 -20~20 사이 숫자나 off 여야 해 -6 을 씁니다", rel)),
        }
    }
    let mode = ini.s("Worker", "StartMode").to_lowercase();
    if mode == "boot" || mode == "lazy" {
        s.start_mode = mode;
    } else if !mode.is_empty() {
        w.push(format!("[Worker] StartMode = {} 를 알 수 없어 boot 를 씁니다", mode));
    }
    s.idle_unload_min = ini.int("Worker", "IdleUnloadMin", 60, 0, 1440, &mut w);
    s.start_timeout_s = ini.int("Worker", "StartTimeoutSec", 180, 10, 1800, &mut w);
    s.answer_timeout_s = ini.int("Worker", "AnswerTimeoutSec", 60, 5, 600, &mut w);
    s.restart_max = ini.int("Worker", "RestartMax", 3, 0, 20, &mut w);
    s.monitor_mode = ini.int("Monitor", "Mode", 0, 0, 1, &mut w);
    s.poll_ms = ini.int("Monitor", "PollMs", 100, 20, 1000, &mut w);
    s.input_recent_ms = ini.int("Monitor", "InputRecentMs", 1500, 100, 10000, &mut w);
    s.panel_enabled = ini.boolean("Panel", "Enabled", true, &mut w);
    s.shrink_explorer = ini.boolean("Panel", "ShrinkExplorer", true, &mut w);
    s.folder_poll_ms = ini.int("Panel", "FolderPollMs", 500, 100, 5000, &mut w);
    let sc = ini.s("Panel", "SearchScope").to_lowercase();
    if sc == "folder" || sc == "root" {
        s.panel_search_scope = sc;
    } else if !sc.is_empty() {
        w.push(format!("[Panel] SearchScope = {} 를 알 수 없어 folder 를 씁니다", sc));
    }
    s.searchbox_trigger = ini.boolean("Trigger", "SearchBox", false, &mut w);
    if let Some(p) = ini.get("Trigger", "Prefix") {
        let p = p.trim();
        s.prefix = if p.is_empty() { "?".into() } else { p.to_string() };
    }
    s.min_chars = ini.int("Trigger", "MinChars", 2, 1, 100, &mut w);
    s.dedup_sec = ini.int("Trigger", "DedupSec", 3, 0, 60, &mut w);
    s.scope_folders = ini.s("Scope", "Folders").split(';').map(|p| p.trim().to_string()).filter(|p| !p.is_empty()).collect();
    s.scope_recheck_min = ini.int("Scope", "RecheckMin", 5, 1, 120, &mut w);
    let dock = ini.s("Window", "Dock").to_lowercase();
    if dock == "off" || dock == "right" {
        s.dock = dock;
    } else if !dock.is_empty() {
        w.push(format!("[Window] Dock = {} 를 알 수 없어 off 를 씁니다", dock));
    }
    s.win_width = ini.int("Window", "Width", 460, 300, 1200, &mut w);
    s.win_max_height = ini.int("Window", "MaxHeight", 560, 200, 2000, &mut w);
    s.font_size = ini.int("Window", "FontSize", 10, 7, 20, &mut w);
    s.autoindex_enabled = ini.boolean("AutoIndex", "Enabled", true, &mut w);
    if !ini.s("AutoIndex", "Folders").is_empty() {
        w.push("[AutoIndex] Folders 는 더 이상 쓰지 않습니다 — [Scope] Folders 하나로 인덱싱·패널 폴더를 정합니다".into());
    }
    s.autoindex_quiet_s = ini.int("AutoIndex", "QuietSec", 5, 1, 300, &mut w);
    s.autoindex_rescan_min = ini.int("AutoIndex", "RescanMin", 10, 0, 1440, &mut w);
    s.autoindex_ask_above = ini.int("AutoIndex", "AskAboveDocs", 50, 0, 100000, &mut w);
    s.autoindex_bm25_idle_s = ini.int("AutoIndex", "Bm25IdleSec", 10, 0, 3600, &mut w);
    let lv = ini.s("Log", "Level");
    s.log_level = if lv.is_empty() { "INFO".into() } else { lv };
    s.warnings = w;
    s
}

//------------------------------------------------------------------
// 워커 명령 찾기 — INI 지정 → exe 옆 simplerag-rs.exe → ..\dist-rust\simplerag-rs.exe
// -out: Ok((명령, 어디서 찾았나)) / Err(안내 문구)
//------------------------------------------------------------------
pub fn find_worker_cmd(s: &Settings) -> Result<(Vec<String>, String), String> {
    if !s.simplerag_exe.is_empty() {
        if std::path::Path::new(&s.simplerag_exe).is_file() {
            return Ok((vec![s.simplerag_exe.clone()], "INI 지정".into()));
        }
        return Err(format!("INI 의 SimpleRagExe 경로에 파일이 없습니다: {}", s.simplerag_exe));
    }
    let app = app_dir();
    let near = format!("{}\\simplerag-rs.exe", app);
    if std::path::Path::new(&near).is_file() {
        return Ok((vec![near], "RAGSearchBox-rs 옆".into()));
    }
    let up = std::path::Path::new(&app).parent().map(|p| p.join("dist-rust").join("simplerag-rs.exe"));
    if let Some(d) = up {
        if d.is_file() {
            return Ok((vec![d.to_string_lossy().into_owned()], "dist-rust 폴더".into()));
        }
    }
    Err("SimpleRAG 실행 파일(simplerag-rs.exe)을 찾지 못했습니다 — INI 의 SimpleRagExe 를 지정하세요".into())
}

//------------------------------------------------------------------
// INI 값 하나 고쳐 쓰기 — 그 줄만 바꾸고(들여쓰기 유지), 없으면 섹션 아래에 넣는다
// -out: Ok(값) / Err(사유)
//------------------------------------------------------------------
pub fn save_ini_value(path: &str, section: &str, key: &str, value: &str) -> Result<String, String> {
    if path.is_empty() || !std::path::Path::new(path).is_file() {
        return Err(format!("설정 파일이 없어 저장하지 않습니다: {}", path));
    }
    let text = std::fs::read(path).map(|b| String::from_utf8_lossy(&b).into_owned()).map_err(|e| format!("설정 파일을 읽지 못했습니다: {}", e))?;
    let bom = text.starts_with('\u{feff}');
    let mut lines: Vec<String> = text.trim_start_matches('\u{feff}').lines().map(|s| s.to_string()).collect();
    let want = format!("[{}]", section.to_lowercase());
    let (mut in_sec, mut done, mut sec_at) = (false, false, None);
    for i in 0..lines.len() {
        let stripped = lines[i].trim().to_string();
        if stripped.starts_with('[') {
            if in_sec && !done {
                break;
            }
            in_sec = stripped.to_lowercase() == want;
            if in_sec {
                sec_at = Some(i);
            }
            continue;
        }
        if !in_sec || stripped.starts_with(';') || stripped.starts_with('#') {
            continue;
        }
        if let Some((k, _)) = stripped.split_once('=') {
            if k.trim().to_lowercase() == key.to_lowercase() {
                let head = lines[i].split_once('=').unwrap().0.to_string();
                lines[i] = format!("{}= {}", head, value);
                done = true;
                break;
            }
        }
    }
    if !done {
        let entry = format!("{:<17}= {}", key, value);
        match sec_at {
            Some(at) => lines.insert(at + 1, entry),
            None => {
                lines.push(String::new());
                lines.push(format!("[{}]", section));
                lines.push(entry);
            }
        }
    }
    let mut out = lines.join("\n") + "\n";
    if bom {
        out = format!("\u{feff}{}", out);
    }
    std::fs::write(path, out).map_err(|e| format!("설정 파일에 쓰지 못했습니다: {}", e))?;
    Ok(value.to_string())
}

//------------------------------------------------------------------
// 패널 너비 저장(300~1200)
//------------------------------------------------------------------
pub fn save_window_width(path: &str, width: i64) -> Result<String, String> {
    if !(300..=1200).contains(&width) {
        return Err(format!("허용 범위(300~1200) 밖이라 저장하지 않습니다: {}", width));
    }
    save_ini_value(path, "Window", "Width", &width.to_string())
}
