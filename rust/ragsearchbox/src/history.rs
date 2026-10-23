//------------------------------------------------------------------
// 대화 기록 (Python RAGSearchBox/history.py)
//=> 최근 대화 10개(대화마다 20마디)를 %LOCALAPPDATA%\RAGSearchBox-rs\history.json 에 남긴다.
//   모양: [{"id", "title", "folder", "updated", "turns": [{"q","scope","evidence","answer","meta",…}]}]
//------------------------------------------------------------------

use serde_json::{json, Value};

pub const MAX_CONVS: usize = 10;
pub const MAX_TURNS: usize = 20;

//------------------------------------------------------------------
// 기록 파일 위치 — 로그 폴더의 부모
//------------------------------------------------------------------
pub fn default_path() -> String {
    let d = crate::log::log_dir();
    let p = std::path::Path::new(&d).parent().map(|p| p.join("history.json")).unwrap_or_else(|| "history.json".into());
    p.to_string_lossy().into_owned()
}

//------------------------------------------------------------------
// 읽기 — 깨졌으면 빈 목록
//------------------------------------------------------------------
pub fn load(path: &str) -> Vec<Value> {
    let raw = match std::fs::read(path) {
        Ok(b) => b,
        Err(_) => return vec![],
    };
    match serde_json::from_slice::<Value>(&raw) {
        Ok(Value::Array(a)) => a.into_iter().filter(|c| c.get("turns").map(|t| t.is_array()).unwrap_or(false)).take(MAX_CONVS).collect(),
        _ => vec![],
    }
}

//------------------------------------------------------------------
// 쓰기(원자적)
//------------------------------------------------------------------
pub fn save(path: &str, convs: &[Value]) -> bool {
    if let Some(d) = std::path::Path::new(path).parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let tmp = format!("{}.tmp", path);
    let data = Value::Array(convs.iter().take(MAX_CONVS).cloned().collect());
    std::fs::write(&tmp, serde_json::to_string(&data).unwrap_or_default()).is_ok() && std::fs::rename(&tmp, path).is_ok()
}

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

// (새 대화 만들기는 패널 화면(JS)이 한다 — 제목은 질문 앞 40자)

//------------------------------------------------------------------
// 대화를 맨 앞으로 올리고 한도를 지킨다
//------------------------------------------------------------------
pub fn touch(convs: &mut Vec<Value>, conv: &mut Value) {
    conv["updated"] = json!(now());
    if let Some(t) = conv["turns"].as_array_mut() {
        if t.len() > MAX_TURNS {
            let cut = t.len() - MAX_TURNS;
            t.drain(..cut);
        }
    }
    let id = conv["id"].clone();
    convs.retain(|c| c["id"] != id);
    convs.insert(0, conv.clone());
    convs.truncate(MAX_CONVS);
}

//------------------------------------------------------------------
// 메뉴에 보일 한 줄 — "제목  ·  10/23 15:20"
//------------------------------------------------------------------
pub fn menu_label(conv: &Value) -> String {
    let title = conv["title"].as_str().unwrap_or("");
    let ts = conv["updated"].as_f64().unwrap_or(0.0);
    match chrono::DateTime::from_timestamp(ts as i64, 0) {
        Some(d) => format!("{}  ·  {}", title, d.with_timezone(&chrono::Local).format("%m/%d %H:%M")),
        None => title.to_string(),
    }
}
