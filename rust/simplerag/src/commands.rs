//------------------------------------------------------------------
// 워커 인덱싱 명령 (Python index/commands.py)
//=> 모델을 올려 둔 채 도는 chat 워커가 stdin 으로 받은 인덱싱 명령을 처리한다.
//   인덱스 폴더는 한 프로세스만 열 수 있으므로, 인덱스를 쥔 워커가 직접 반영한다.
//     보내기: /index-doc {"path": "D:\\분류함\\규정.docx"}   (경로만 써도 된다)
//     받기  : @index {"op": "index-doc", "ok": true, ...}    (한 줄 ASCII JSON)
//------------------------------------------------------------------

use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::app::App;
use crate::indexer::{self, docs};
use crate::pyutil;

pub const PREFIX: &str = "@index ";
pub const COMMANDS: &[&str] = &["/index-doc", "/remove-doc", "/bm25", "/index-plan", "/index-outside"];

//------------------------------------------------------------------
// 인자에서 경로 꺼내기 — JSON {"path": …} 이면 그 값, 아니면 글자 그대로(따옴표 제거)
//------------------------------------------------------------------
pub fn parse_path(arg: &str) -> String {
    let a = pyutil::strip(arg);
    if a.starts_with('{') {
        if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(a) {
            return match m.get("path") {
                Some(Value::String(s)) => pyutil::strip(s).to_string(),
                Some(Value::Null) | None => String::new(),
                Some(v) => pyutil::strip(&v.to_string()).to_string(),
            };
        }
    }
    pyutil::strip_chars(a, "\"").to_string()
}

// 인자 JSON 의 값 하나
pub fn parse_value(arg: &str, key: &str) -> Option<Value> {
    let a = pyutil::strip(arg);
    if !a.starts_with('{') {
        return None;
    }
    match serde_json::from_str::<Value>(a) {
        Ok(Value::Object(m)) => m.get(key).cloned(),
        _ => None,
    }
}

fn truthy(v: Option<Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

//------------------------------------------------------------------
// 결과 한 줄 — prefix + 한 줄 ASCII JSON
//------------------------------------------------------------------
pub fn format_result(result: &Map<String, Value>, prefix: &str) -> String {
    format!("{}{}", prefix, pyutil::json_ascii(&Value::Object(result.clone()), true))
}

//------------------------------------------------------------------
// 명령 한 줄 처리 — 무슨 일이 있어도 결과 사전(op·ok·ms)을 돌려준다
//------------------------------------------------------------------
pub fn run(app: &App, cmd: &str, arg: &str) -> Map<String, Value> {
    let t0 = Instant::now();
    let op = cmd.trim_start_matches('/').to_string();
    let r: Result<Map<String, Value>, String> = match cmd {
        "/index-doc" => index_doc(app, &parse_path(arg)),
        "/remove-doc" => remove_doc(app, &parse_path(arg), parse_value(arg, "outside_of")),
        "/index-outside" => Ok(outside(&parse_value(arg, "roots").and_then(|v| v.as_array().cloned()).unwrap_or_default())),
        "/bm25" => rebuild_bm25(app),
        "/index-plan" => Ok(plan(&parse_path(arg), truthy(parse_value(arg, "allow_delete")))),
        _ => Ok(obj(json!({"ok": false, "why": "알 수 없는 명령"}))),
    };
    let res = r.unwrap_or_else(|e| {
        let (k, m) = match e.split_once(": ") {
            Some((k, m)) if k.ends_with("Error") && !k.contains(' ') => (k.to_string(), m.to_string()),
            _ => ("RuntimeError".to_string(), e.clone()),
        };
        obj(json!({"ok": false, "why": format!("{}: {}", k, pyutil::head(&m, 120))}))
    });
    let mut out = Map::new();
    out.insert("op".into(), json!(op));
    for (k, v) in res {
        out.insert(k, v);
    }
    out.insert("ms".into(), json!(t0.elapsed().as_millis() as i64));
    out
}

fn obj(v: Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

// 상태 파일 읽고 청킹 설정 확인
fn state() -> Result<indexer::State, String> {
    let mut s = indexer::load_state();
    indexer::check_chunk_params(&mut s, false, &mut |_| {}).map_err(|e| format!("RuntimeError: {}", e))?;
    Ok(s)
}

//------------------------------------------------------------------
// 문서 한 건 추가·수정
//------------------------------------------------------------------
fn index_doc(app: &App, path: &str) -> Result<Map<String, Value>, String> {
    if path.is_empty() {
        return Ok(obj(json!({"ok": false, "why": "경로가 없습니다"})));
    }
    let path = pyutil::abspath(path);
    if !std::path::Path::new(&path).is_file() {
        return Ok(obj(json!({"ok": false, "path": path, "why": "파일이 없습니다"})));
    }
    if !indexer::TEXT_EXTS.contains(&pyutil::ext_lower(&path).as_str()) || indexer::is_transient(&path) {
        return Ok(obj(json!({"ok": false, "path": path, "why": "인덱싱 대상이 아닌 파일입니다"})));
    }
    let mut st = state()?;
    let fp = indexer::file_fingerprint(&path).map_err(|e| format!("OSError: {}", e))?;
    if let Some(prev) = docs(&st).get(&path) {
        if prev.get("fingerprint").and_then(|v| v.as_str()) == Some(fp.as_str()) {
            return Ok(obj(json!({"ok": true, "path": path, "result": "unchanged", "chunks": prev.get("chunks").cloned().unwrap_or(json!(0))})));
        }
    }
    if let Some(f) = st.get("failed").and_then(|v| v.get(&path)) {
        if f.get("fingerprint").and_then(|v| v.as_str()) == Some(fp.as_str()) {
            return Ok(obj(json!({"ok": false, "path": path, "result": "failed_before", "why": f.get("why").cloned().unwrap_or(json!(""))})));
        }
    }
    app.embedder.ensure_loaded()?;
    let r = indexer::sync_doc(&path, &fp, &mut st, &app.embedder, &app.store, &mut |_| {})?;
    let mut out = obj(json!({"ok": r.ok, "path": path, "result": r.op, "chunks": r.chunks}));
    if let Some(f) = &r.from {
        out.insert("from".into(), json!(f));
    }
    if !r.ok {
        out.insert("why".into(), json!(r.why));
        out.insert("kept_old".into(), json!(r.kept_old));
    }
    Ok(out)
}

//------------------------------------------------------------------
// 문서 한 건 빼기 — 파일이 아직 있으면 거절(지정 폴더 밖이면 예외)
//------------------------------------------------------------------
fn remove_doc(app: &App, path: &str, outside_of: Option<Value>) -> Result<Map<String, Value>, String> {
    if path.is_empty() {
        return Ok(obj(json!({"ok": false, "why": "경로가 없습니다"})));
    }
    if std::path::Path::new(path).exists() {
        let roots: Vec<String> = outside_of.and_then(|v| v.as_array().cloned()).unwrap_or_default()
            .into_iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect();
        let outside = !roots.is_empty() && !under_any(path, &roots);
        if !outside {
            return Ok(obj(json!({"ok": false, "path": path, "why": "파일이 아직 있어 빼지 않습니다"})));
        }
    }
    let mut st = state()?;
    let want = indexer::norm_path(path);
    let key = docs(&st).keys().find(|k| indexer::norm_path(k) == want).cloned();
    let key = match key {
        Some(k) => k,
        None => return Ok(obj(json!({"ok": true, "path": path, "removed": 0, "result": "not_indexed"}))),
    };
    let n = indexer::remove_doc(&key, &mut st, &app.store)?;
    Ok(obj(json!({"ok": true, "path": key, "removed": n, "result": "removed"})))
}

//------------------------------------------------------------------
// BM25 다시 만들기 + 고아 청소
//------------------------------------------------------------------
fn rebuild_bm25(app: &App) -> Result<Map<String, Value>, String> {
    let mut st = state()?;
    let mut logs: Vec<String> = vec![];
    let (texts, ids, n_orphans) = indexer::collect_for_bm25(&app.store, Some(&st), &mut |m| logs.push(m.to_string()))?;
    let tok = app.embedder.tokenizer()?;
    let build_s = if texts.is_empty() { 0.0 } else { app.bm25.build(&texts, &ids, &tok)? };
    st.insert("bm25_built_at".into(), pyutil::jf(pyutil::now_ts()));
    indexer::save_state(&st)?;
    let mut out = obj(json!({"ok": true, "chunks": texts.len(), "orphans": n_orphans}));
    out.insert("build_s".into(), pyutil::jf(pyutil::round_to(build_s, 2)));
    if !logs.is_empty() {
        let note = logs.iter().map(|l| pyutil::strip(l)).collect::<Vec<_>>().join(" / ");
        out.insert("note".into(), json!(pyutil::head(&note, 300)));
    }
    Ok(out)
}

//------------------------------------------------------------------
// 지정 폴더 밖 문서 목록(인덱스는 안 건드린다)
//------------------------------------------------------------------
fn outside(roots: &[Value]) -> Map<String, Value> {
    let roots: Vec<String> = roots.iter().filter_map(|r| r.as_str()).filter(|s| !s.is_empty()).map(|s| s.to_string()).collect();
    if roots.is_empty() {
        return obj(json!({"ok": false, "why": "지정 폴더가 없습니다"}));
    }
    let st = match state() {
        Ok(s) => s,
        Err(e) => return obj(json!({"ok": false, "why": e})),
    };
    let all: Vec<String> = docs(&st).keys().cloned().collect();
    let out: Vec<String> = all.iter().filter(|k| !under_any(k, &roots)).cloned().collect();
    let (ok, why) = indexer::delete_guard(out.len(), all.len());
    obj(json!({"ok": true, "outside": out, "total": all.len(), "delete_blocked": if ok { String::new() } else { why }}))
}

//------------------------------------------------------------------
// 할 일 목록(인덱스는 안 건드린다)
//------------------------------------------------------------------
fn plan(folder: &str, allow_delete: bool) -> Map<String, Value> {
    if folder.is_empty() || !std::path::Path::new(folder).is_dir() {
        return obj(json!({"ok": false, "folder": folder, "why": "폴더가 보이지 않습니다"}));
    }
    let st = match state() {
        Ok(s) => s,
        Err(e) => return obj(json!({"ok": false, "why": e})),
    };
    let p = match indexer::plan_changes(folder, &st, true, allow_delete, false) {
        Ok(p) => p,
        Err(e) => return obj(json!({"ok": false, "why": format!("OSError: {}", pyutil::head(&e, 120))})),
    };
    let mv: Vec<String> = p.moved.iter().map(|(_, n)| n.clone()).collect();
    let moving: std::collections::HashSet<String> = mv.iter().map(|m| indexer::norm_path(m)).collect();
    let add: Vec<String> = p.todo.iter().filter(|(path, _)| !docs(&st).contains_key(&pyutil::abspath(path)) && !moving.contains(&indexer::norm_path(path)))
        .map(|(p, _)| p.clone()).collect();
    let modify: Vec<String> = p.todo.iter().filter(|(path, _)| docs(&st).contains_key(&pyutil::abspath(path))).map(|(p, _)| p.clone()).collect();
    obj(json!({"ok": true, "folder": pyutil::abspath(folder), "files": p.files.len(), "add": add, "modify": modify,
               "move": mv, "remove": p.removed, "delete_blocked": p.delete_blocked, "retry_skip": p.retry_skip,
               "bm25_stale": indexer::bm25_stale(&st)}))
}

//------------------------------------------------------------------
// 경로가 폴더들 가운데 하나 아래인가(하위 포함, 대소문자 무시)
//------------------------------------------------------------------
pub fn under_any(path: &str, roots: &[String]) -> bool {
    let p = indexer::norm_path(path);
    roots.iter().any(|r| p.starts_with(&format!("{}\\", pyutil::rstrip_chars(&indexer::norm_path(r), "\\/"))))
}
