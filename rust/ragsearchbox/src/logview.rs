//------------------------------------------------------------------
// 설정 > 로그 판 (로그 보기 설계서 plan/로그보기_설계서_20261112.html)
//=> 두 가지를 모아 설정 창에 넘긴다.
//    1) 인덱싱 실패 문서 — 워커 데이터 폴더의 index_state.json "failed" 묶음(지금 실패 중인 문서의 정답 목록)
//       로그를 긁지 않는 까닭: 고쳐진 문서가 남고, 폴더 전체 index 명령의 실패는 이름이 없고, 돌려 쓰기로 빠진다.
//    2) 로그 경고·오류 — 최근 7일 MoAISearch_날짜.log 의 WARNI·ERROR 줄 + 워커 로그의 오류로 보이는 줄,
//       같은 내용은 한 묶음(횟수)으로.
//   상태 파일은 워커가 write_atomic 으로 바꿔 끼우므로 읽기만 하면 반쯤 쓴 파일을 볼 일이 없다.
//------------------------------------------------------------------

use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime};

use serde_json::{json, Value};

pub const DAYS: i64 = 7;
// 파일마다 끝에서 이만큼만 읽는다(돌려 쓰기 한도 5MB 안)
const TAIL_BYTES: u64 = 2 * 1024 * 1024;
const MAX_GROUPS: usize = 500;
const MAX_LINES: usize = 20;
// 파일이 있는지 보는 데 쓸 시간 — 끊긴 네트워크 폴더가 많아도 판이 늦게 뜨지 않게
const EXISTS_BUDGET: Duration = Duration::from_millis(1500);

//------------------------------------------------------------------
// 실패 원인 풀이 한 건
// -필드: kind  = 묶음 열쇠(알약 거르기용)
// -필드: title = 보일 원인(쉬운 말)
// -필드: hint  = 할 일 안내 한 줄
// -필드: retry = 다시 시도가 쓸모 있을 원인인가(파일 잠김 · 시간 초과 등)
//------------------------------------------------------------------
#[derive(Debug, PartialEq)]
pub struct Reason {
    pub kind: &'static str,
    pub title: &'static str,
    pub hint: &'static str,
    pub retry: bool,
}

//------------------------------------------------------------------
// 사유 원문 → 쉬운 말 (설계서 §4-4)
//=> 위에서부터 처음 맞는 규칙을 고른다. 사유는 why_of() 가 80자로 자르므로 앞쪽 글자로만 맞춘다.
//    운영 상태 파일 실측(2026-11-12, 실패 214건): pdf:empty 175 · 감지=unknown 31 · 감지=ole 7 · 텍스트 없음 1
// -in: why  = 상태 파일의 why 원문
// -in: path = 문서 경로(확장자로 Office 암호 문서를 가른다)
// -out: Reason
// -out: error = 없음 (못 맞추면 "추출 실패")
//------------------------------------------------------------------
pub fn explain(why: &str, path: &str) -> Reason {
    let ext = Path::new(path).extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
    let has = |k: &str| why.contains(k);
    if has("pdf:empty") {
        return Reason { kind: "scan_pdf", title: "글자 없는 PDF",
            hint: "스캔·이미지로만 된 PDF 라 글자를 뽑지 못했습니다. OCR 로 글자를 넣은 PDF 로 바꾸면 다시 넣습니다. 이름·날짜로는 지금도 찾을 수 있습니다.", retry: false };
    }
    // 암호 걸린 docx·xlsx·pptx 는 OLE 껍데기에 싸여 있어 감지=ole 로 나온다
    if has("암호") || has("Encrypt") || has("password") || (has("감지=ole") && ["docx", "xlsx", "pptx"].contains(&ext.as_str())) {
        return Reason { kind: "locked", title: "암호 걸린 문서",
            hint: "암호(또는 문서 보안)가 걸려 있어 열지 못했습니다. 암호를 풀어 저장하면 다음 대조 때 저절로 다시 넣습니다.", retry: false };
    }
    if has("크기 상한 초과") {
        return Reason { kind: "too_big", title: "파일이 너무 큼",
            hint: "크기 상한(MAX_FILE_BYTES)보다 큽니다. 문서를 나누거나 상한을 늘리세요.", retry: false };
    }
    if has("OSError") || has("읽기 실패") {
        return Reason { kind: "unreadable", title: "파일을 열 수 없음",
            hint: "다른 프로그램이 쓰고 있거나, 권한이 없거나, 네트워크 폴더가 끊겼습니다. 풀린 뒤 다시 시도하세요.", retry: true };
    }
    // 확장자는 문서인데 형식을 못 알아봄 — 회사 문서 보안(DRM)으로 암호화됐거나 깨진 파일인 경우가 많다
    if has("감지=unknown") || has("감지=ole") {
        return Reason { kind: "unknown_format", title: "형식을 알 수 없음",
            hint: "확장자와 내용이 맞지 않습니다. 문서 보안(DRM)으로 암호화됐거나 손상된 파일일 수 있습니다. 문서를 열어 다시 저장해 보세요.", retry: false };
    }
    if has("사용 가능한 추출기 없음") {
        return Reason { kind: "unsupported", title: "지원하지 않는 형식",
            hint: "글자를 뽑을 수 없는 형식입니다. 설정 > 기본 > 인덱싱 확장자에서 빼도 됩니다.", retry: false };
    }
    if has("추출 텍스트 없음") || has(":empty") {
        return Reason { kind: "empty", title: "본문이 비어 있음",
            hint: "빈 문서이거나 그림만 있습니다.", retry: false };
    }
    if has("timeout") || has("시간 한도") || has("시간 초과") {
        return Reason { kind: "timeout", title: "시간 초과",
            hint: "글자를 뽑는 데 너무 오래 걸렸습니다. 쉴 때 다시 시도해 보세요.", retry: true };
    }
    Reason { kind: "other", title: "추출 실패", hint: "원래 사유를 펼쳐 보세요.", retry: true }
}

// 상태 파일 캐시 — (수정 시각, 크기)가 같으면 다시 파싱하지 않는다(파일이 2MB 남짓)
struct Cache {
    path: String,
    sig: Option<(SystemTime, u64)>,
    failed: Vec<(String, Value)>,
}
static CACHE: Mutex<Option<Cache>> = Mutex::new(None);

//------------------------------------------------------------------
// 상태 파일의 failed 묶음 꺼내기(캐시)
//=> 1) 수정 시각 · 크기를 보고 바뀌지 않았으면 지난 결과를 쓴다
//    2) 바뀌었으면 읽어 failed 만 꺼내 둔다
// -in: state_path = index_state.json 경로
// -out: [(경로, 항목)] — 파일이 없거나 깨졌으면 빈 목록
// -out: error = 없음
//------------------------------------------------------------------
fn failed_entries(state_path: &str) -> Vec<(String, Value)> {
    let sig = std::fs::metadata(state_path).ok().and_then(|m| Some((m.modified().ok()?, m.len())));
    let mut g = CACHE.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(c) = g.as_ref() {
        if c.path == state_path && c.sig == sig && sig.is_some() {
            return c.failed.clone();
        }
    }
    let failed: Vec<(String, Value)> = std::fs::read(state_path).ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| v.get("failed").and_then(|f| f.as_object()).cloned())
        .map(|m| m.into_iter().collect())
        .unwrap_or_default();
    *g = Some(Cache { path: state_path.to_string(), sig, failed: failed.clone() });
    failed
}

// 메뉴 "로그 (N)" 용 — 1초마다 불려도 상태 파일이 바뀔 때만 다시 센다
pub fn count_failed(state_path: &str) -> usize {
    failed_entries(state_path).len()
}

//------------------------------------------------------------------
// 실패 문서 목록 — 설정 창이 그릴 모양으로
//=> 1) failed 항목마다 원인 풀이를 붙인다
//    2) 최근 실패가 위(at 없는 옛 항목은 아래, 이름 순)
//    3) 파일이 아직 있는지 본다 — 시간 한도를 넘으면 나머지는 모름(null)
// -in: state_path = index_state.json 경로
// -out: [{path,name,dir,kind,title,hint,why,at,kept_old,exists,retry}]
// -out: error = 없음
//------------------------------------------------------------------
pub fn failed_docs(state_path: &str) -> Vec<Value> {
    let mut rows: Vec<(String, Value)> = failed_entries(state_path);
    rows.sort_by(|a, b| {
        let at = |v: &Value| v.get("at").and_then(|x| x.as_str()).unwrap_or("").to_string();
        at(&b.1).cmp(&at(&a.1)).then_with(|| a.0.to_lowercase().cmp(&b.0.to_lowercase()))
    });
    let t0 = Instant::now();
    rows.into_iter().map(|(path, v)| {
        let why = v.get("why").and_then(|x| x.as_str()).unwrap_or("").to_string();
        let r = explain(&why, &path);
        let name = v.get("name").and_then(|x| x.as_str()).map(|s| s.to_string())
            .unwrap_or_else(|| Path::new(&path).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
        let dir = Path::new(&path).parent().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
        // 끊긴 네트워크 폴더는 한 번에 몇 초씩 걸릴 수 있어 시간 한도 안에서만 본다
        let exists = if t0.elapsed() < EXISTS_BUDGET { json!(Path::new(&path).exists()) } else { Value::Null };
        json!({"path": path, "name": name, "dir": dir, "kind": r.kind, "title": r.title, "hint": r.hint, "why": why,
               "at": v.get("at").cloned().unwrap_or(Value::Null), "kept_old": v.get("kept_old").and_then(|x| x.as_bool()).unwrap_or(false),
               "exists": exists, "retry": r.retry})
    }).collect()
}

//------------------------------------------------------------------
// 로그 한 줄 나누기 — "YYYY-MM-DD HH:MM:SS 단계 rsb.이름 내용"
//=> 단계 칸은 5글자(INFO␠ · WARNI · ERROR · DIAG␠ · DEBUG)라 그 뒤 공백 수가 다르다.
// -in: line = 로그 한 줄
// -out: Some((시각, 단계, 곳, 내용)) / 시각으로 시작하지 않으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn parse_line(line: &str) -> Option<(String, String, String, String)> {
    let b = line.as_bytes();
    if b.len() < 26 || b[4] != b'-' || b[10] != b' ' || b[13] != b':' || !b[0].is_ascii_digit() {
        return None;
    }
    let ts = line.get(..19)?.to_string();
    let lv = line.get(20..25)?.trim().to_string();
    let rest = line.get(25..)?.trim_start();
    let (src, msg) = rest.split_once(' ').unwrap_or((rest, ""));
    let src = src.strip_prefix("rsb.").unwrap_or(src);
    Some((ts, lv, src.to_string(), msg.to_string()))
}

// 묶음 열쇠의 내용 틀 — 숫자만 # 로(경로는 남겨 폴더마다 따로 묶이게)
fn shape(msg: &str) -> String {
    let mut out = String::with_capacity(msg.len());
    let mut prev_hash = false;
    for c in msg.chars().take(200) {
        if c.is_ascii_digit() {
            if !prev_hash {
                out.push('#');
            }
            prev_hash = true;
        } else {
            out.push(c);
            prev_hash = false;
        }
    }
    out
}

// 워커 로그에서 오류로 볼 줄 — 워커 로그는 단계 없이 늘 INFO 다
fn worker_is_issue(msg: &str) -> bool {
    ["panicked", "error", "Error", "ERROR", "오류", "실패"].iter().any(|k| msg.contains(k))
}

// 인덱싱 실패 줄 — 실패 문서 묶음과 겹치므로 화면에서 기본으로 숨긴다
fn is_index_fail(src: &str, msg: &str) -> bool {
    (src == "autoindex" && (msg.starts_with("반영 실패") || msg.starts_with("index 명령 실패"))) || msg.starts_with("[index]")
}

// 파일 끝에서 최대 TAIL_BYTES 읽기(앞쪽 잘린 줄은 버린다)
fn read_tail(path: &Path) -> String {
    let mut f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return String::new(),
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(TAIL_BYTES);
    if f.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut buf = vec![];
    let _ = f.read_to_end(&mut buf);
    let s = String::from_utf8_lossy(&buf).into_owned();
    if start > 0 {
        s.split_once('\n').map(|x| x.1.to_string()).unwrap_or_default()
    } else {
        s
    }
}

// 한 묶음 — 같은 (단계, 곳, 내용 틀)
struct Group {
    level: String,
    src: String,
    text: String,
    first: String,
    last: String,
    count: usize,
    index_fail: bool,
    lines: Vec<String>,
}

//------------------------------------------------------------------
// 로그 경고·오류 모으기 (설계서 §5)
//=> 1) 오늘부터 days 일 동안의 프로그램 · 워커 로그(돌려 쓴 .3 .2 .1 → 본 파일 순)를 끝 2MB 씩 읽는다
//    2) 프로그램 로그는 WARNI · ERROR, 워커 로그는 오류로 보이는 줄만
//    3) "  stderr: …" 처럼 바로 앞 경고에 딸린 줄은 앞 묶음의 원래 줄로 붙인다
//    4) (단계, 곳, 내용 틀)이 같으면 한 묶음 — 마지막 시각 · 횟수 · 대표 줄(마지막 원문)
// -in: log_dir = 로그 폴더
// -in: dates   = 읽을 날짜들("YYYY-MM-DD", 오래된 것부터)
// -out: [{level,src,text,first,last,count,index_fail,lines}] — 마지막 시각이 최근인 것부터, 최대 500
// -out: error = 없음 (못 읽는 파일은 건너뜀)
//------------------------------------------------------------------
pub fn read_issues(log_dir: &str, dates: &[String]) -> (Vec<Value>, bool) {
    let mut groups: HashMap<(String, String, String), Group> = HashMap::new();
    for date in dates {
        for (base, is_worker) in [(crate::log::MAIN_BASE, false), (crate::log::WORKER_BASE, true)] {
            let main = Path::new(log_dir).join(crate::log::dated_name(base, date));
            let mut files: Vec<std::path::PathBuf> = (1..=3).rev().map(|i| std::path::PathBuf::from(format!("{}.{}", main.display(), i))).collect();
            files.push(main);
            for f in files.iter().filter(|f| f.exists()) {
                let mut prev_key: Option<(String, String, String)> = None;
                for line in read_tail(f).lines() {
                    let (ts, lv, src, msg) = match parse_line(line) {
                        Some(x) => x,
                        None => continue,
                    };
                    let (level, src) = if is_worker {
                        if !worker_is_issue(&msg) {
                            prev_key = None;
                            continue;
                        }
                        ("WARNI".to_string(), "worker".to_string())
                    } else if lv == "WARNI" || lv == "ERROR" {
                        (lv, src)
                    } else {
                        prev_key = None;
                        continue;
                    };
                    // 앞 경고에 딸린 줄(워커 종료 뒤 stderr 들) — 따로 묶지 않고 앞 묶음에 원문만 붙인다
                    if msg.starts_with("  ") {
                        if let Some(g) = prev_key.as_ref().and_then(|k| groups.get_mut(k)) {
                            if g.src == src && g.lines.len() < MAX_LINES {
                                g.lines.push(format!("{} {}", ts, msg.trim()));
                            }
                            continue;
                        }
                    }
                    let key = (level.clone(), src.clone(), shape(&msg));
                    let g = groups.entry(key.clone()).or_insert_with(|| Group {
                        level: level.clone(), src: src.clone(), text: String::new(), first: ts.clone(), last: String::new(),
                        count: 0, index_fail: is_index_fail(&src, &msg), lines: vec![],
                    });
                    g.count += 1;
                    if ts < g.first {
                        g.first = ts.clone();
                    }
                    if ts >= g.last {
                        g.last = ts.clone();
                        g.text = msg.clone();
                    }
                    // 원래 줄은 최근 것만 남긴다
                    if g.lines.len() >= MAX_LINES {
                        g.lines.remove(0);
                    }
                    g.lines.push(format!("{} {}", ts, msg));
                    prev_key = Some(key);
                }
            }
        }
    }
    let mut v: Vec<Group> = groups.into_values().collect();
    v.sort_by(|a, b| b.last.cmp(&a.last));
    let more = v.len() > MAX_GROUPS;
    v.truncate(MAX_GROUPS);
    let out = v.into_iter().map(|g| json!({"level": if g.level == "ERROR" { "error" } else { "warn" }, "src": g.src, "text": g.text,
        "first": g.first, "last": g.last, "count": g.count, "index_fail": g.index_fail, "lines": g.lines})).collect();
    (out, more)
}

// 오늘부터 거꾸로 days 일(오래된 것부터)
pub fn recent_dates(days: i64) -> Vec<String> {
    let today = chrono::Local::now().date_naive();
    (0..days).rev().map(|i| (today - chrono::Duration::days(i)).format("%Y-%m-%d").to_string()).collect()
}

//------------------------------------------------------------------
// 로그 판 전체 — 설정 창 rsb.logView() 에 넘길 값
// -in: state_path = index_state.json 경로(워커 폴더를 못 찾으면 None)
// -out: {failed, issues, issues_more, state_found, log_dir, files:{main,worker}, level, days}
// -out: error = 없음
//------------------------------------------------------------------
pub fn build(state_path: Option<&str>) -> Value {
    let dir = crate::log::log_dir();
    let (issues, more) = read_issues(&dir, &recent_dates(DAYS));
    let found = state_path.map(|p| Path::new(p).exists()).unwrap_or(false);
    let failed = match state_path {
        Some(p) if found => failed_docs(p),
        _ => vec![],
    };
    let (main, worker) = crate::log::today_names();
    json!({"failed": failed, "issues": issues, "issues_more": more, "state_found": found, "log_dir": dir,
           "files": {"main": main, "worker": worker}, "level": crate::log::level_name(), "days": DAYS})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explain_rules() {
        // 붙여 주신 실패 줄 그대로
        let r = explain("ExtractError: 하이브리드 전엔진 실패(감지=pdf; pdf:empty)", r"D:\분류함\영업\채널계약\채널사협력계약(카파정보기술_230315).pdf");
        assert_eq!(r.kind, "scan_pdf");
        assert_eq!(explain("ExtractError: 사용 가능한 추출기 없음(감지=ole)", "a.pptx").kind, "locked");
        assert_eq!(explain("ExtractError: 사용 가능한 추출기 없음(감지=unknown)", "a.pdf").kind, "unknown_format");
        assert_eq!(explain("ExtractError: 사용 가능한 추출기 없음(감지=zip)", "a.zip").kind, "unsupported");
        assert_eq!(explain("ValueError: 추출 텍스트 없음", "a.pptx").kind, "empty");
        assert_eq!(explain("ExtractError: 크기 상한 초과: 300MB > 200MB (MAX_FILE_BYTES)", "a.pdf").kind, "too_big");
        assert_eq!(explain("ExtractError: HWP5 암호화 문서", "a.hwp").kind, "locked");
        assert!(explain("ExtractError: 이상한 일", "a.pdf").retry);
    }

    #[test]
    fn parse_lines() {
        let (ts, lv, src, msg) = parse_line("2026-11-12 12:57:12 WARNI rsb.autoindex 반영 실패 index-doc: D:\\a.pdf — \"x\"").unwrap();
        assert_eq!((ts.as_str(), lv.as_str(), src.as_str()), ("2026-11-12 12:57:12", "WARNI", "autoindex"));
        assert!(msg.starts_with("반영 실패"));
        let (_, lv, src, msg) = parse_line("2026-11-12 12:57:12 INFO  rsb.worker 상태: ready").unwrap();
        assert_eq!((lv.as_str(), src.as_str(), msg.as_str()), ("INFO", "worker", "상태: ready"));
        let (_, _, src, _) = parse_line("2026-11-12 10:39:29 INFO  rsbworker 리랭커 적재 완료 (1323ms)").unwrap();
        assert_eq!(src, "rsbworker");
        assert!(parse_line("  이어진 줄").is_none());
        assert!(parse_line("20").is_none());
    }

    #[test]
    fn groups_and_children() {
        let dir = std::env::temp_dir().join(format!("rsb_logview_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let d = "2026-11-12".to_string();
        let main = dir.join(crate::log::dated_name(crate::log::MAIN_BASE, &d));
        std::fs::write(&main, "\
2026-11-12 08:00:00 WARNI rsb.watcher 폴더를 지켜볼 수 없다(60초 뒤 다시): \\\\NAS\\a — 끊김
2026-11-12 08:01:00 WARNI rsb.watcher 폴더를 지켜볼 수 없다(60초 뒤 다시): \\\\NAS\\a — 끊김
2026-11-12 08:02:00 INFO  rsb.app 보통 줄
2026-11-12 09:00:00 WARNI rsb.worker 워커가 종료됨 code=101 (답변 중이던 질문: None)
2026-11-12 09:00:00 WARNI rsb.worker   stderr: thread 'main' panicked
2026-11-12 09:30:00 WARNI rsb.autoindex 반영 실패 index-doc: D:\\a.pdf — \"x\"
2026-11-12 10:00:00 ERROR rsb.app 오류 하나
").unwrap();
        let worker = dir.join(crate::log::dated_name(crate::log::WORKER_BASE, &d));
        std::fs::write(&worker, "2026-11-12 10:39:29 INFO  rsbworker 리랭커 적재 완료 (1323ms)\n2026-11-12 11:00:00 INFO  rsbworker thread 'x' panicked at a.rs\n").unwrap();
        let (v, more) = read_issues(&dir.to_string_lossy(), &[d]);
        let _ = std::fs::remove_dir_all(&dir);
        assert!(!more);
        assert_eq!(v.len(), 5, "{:?}", v);
        assert_eq!(v[0]["src"], "worker");
        assert_eq!(v[1]["level"], "error");
        let w = v.iter().find(|g| g["src"] == "watcher").unwrap();
        assert_eq!(w["count"], 2);
        let k = v.iter().find(|g| g["text"].as_str().unwrap().starts_with("워커가 종료됨")).unwrap();
        assert_eq!(k["lines"].as_array().unwrap().len(), 2);
        assert!(v.iter().any(|g| g["index_fail"] == true));
    }
}
