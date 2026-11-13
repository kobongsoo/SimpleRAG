//------------------------------------------------------------------
// SimpleRAG config.yaml 의 generation 한 줄 읽기·바꾸기 — 답변 모델(model) · 이어서 묻기(multiturn) · 옛 정밀 모드(precision)
//=> 워커는 시작할 때 config.yaml 을 읽으므로 바꾼 뒤 모델을 다시 올린다.
//   yaml 로 다시 쓰면 설명 주석이 날아가므로 generation 구역의 그 줄 값만 갈아 끼운다(줄 끝 CRLF/LF 보존).
//   예전 이름은 precision.rs(정밀 모드 켜고 끄기) — 정밀 모드는 설정 > 모델로 바뀌었다(모델 받기 설계서 §8)
//------------------------------------------------------------------

use std::path::Path;

// 0.1.3 까지 정밀 모드가 쓰던 모델 — 옮겨 가기(migrate_precision)에 쓴다
pub const OLD_PRECISION_ALIAS: &str = "qwen3-4b-q4";
pub const OLD_PRECISION_FILE: &str = "Qwen3-4B-Q4_K_M.gguf";
// 멀티턴 안내 글 (Python precision.MULTITURN_NOTE 와 같다)
pub const MULTITURN_NOTE: &str = "따라 묻는 질문(\"침구류는?\")의 빠진 주제를 같은 대화의 바로 앞 질문에서 채워 찾습니다. 새 대화(✎)를 누르면 끊깁니다. 바꾸면 모델을 다시 올립니다.";

// 키의 기본값 — precision 끔 · multiturn 켬
fn default_of(key: &str) -> bool {
    key == "multiturn"
}

//------------------------------------------------------------------
// SimpleRAG 가 읽는 config.yaml — SIMPLERAG_CONFIG → 데이터 폴더 → 프로그램 폴더. 없으면 None
//------------------------------------------------------------------
pub fn config_path(data_home: Option<&str>, app_dir: Option<&str>) -> Option<String> {
    if let Ok(env) = std::env::var("SIMPLERAG_CONFIG") {
        let env = env.trim().to_string();
        if !env.is_empty() && env != "-" {
            return if Path::new(&env).is_file() { Some(env) } else { None };
        }
    }
    for d in [data_home, app_dir].into_iter().flatten() {
        let p = Path::new(d).join("config.yaml");
        if p.is_file() {
            return Some(p.to_string_lossy().into_owned());
        }
    }
    None
}

// "  <key>: 값   # 주석" 줄을 (머리 "  <key>: ", 값, 꼬리) 로 — 들여쓴 줄만(Python _line 과 같다)
fn split_line<'a>(line: &'a str, key: &str) -> Option<(&'a str, &'a str, &'a str)> {
    if !line.starts_with(|c: char| c.is_whitespace()) {
        return None;
    }
    let rest = line.trim_start().strip_prefix(key)?.strip_prefix(':')?;
    let lead = line.len() - rest.len();
    let v_at = lead + (rest.len() - rest.trim_start().len());
    let v_len = line[v_at..].find(char::is_whitespace).unwrap_or(line.len() - v_at);
    if v_len == 0 {
        return None;
    }
    Some((&line[..v_at], &line[v_at..v_at + v_len], &line[v_at + v_len..]))
}

// (generation 줄 번호, 그 키 줄 번호) — 없으면 None
fn find(lines: &[String], key: &str) -> (Option<usize>, Option<usize>) {
    let mut sec = None;
    for (i, line) in lines.iter().enumerate() {
        if !line.is_empty() && !line.starts_with(|c: char| c.is_whitespace()) && !line.starts_with('#') {
            if sec.is_some() {
                break;
            }
            if line.split('#').next().unwrap_or("").trim() == "generation:" {
                sec = Some(i);
            }
            continue;
        }
        if sec.is_some() && split_line(line, key).is_some() {
            return (sec, Some(i));
        }
    }
    (sec, None)
}

//------------------------------------------------------------------
// 그 줄의 값 글자 — 줄이 없거나 못 읽으면 None
//------------------------------------------------------------------
pub fn read_value(path: &str, key: &str) -> Option<String> {
    let raw = std::fs::read_to_string(path).ok()?;
    let lines: Vec<String> = raw.trim_start_matches('\u{feff}').lines().map(String::from).collect();
    let at = find(&lines, key).1?;
    Some(split_line(&lines[at], key)?.1.trim_matches(|c| c == '"' || c == '\'').to_string())
}

//------------------------------------------------------------------
// 지금 켜져 있나 (멀티턴 · 옛 정밀 모드) — 줄이 없거나 못 읽으면 그 키의 기본값
//------------------------------------------------------------------
pub fn read(path: &str, key: &str) -> bool {
    match read_value(path, key) {
        Some(v) => matches!(v.to_lowercase().as_str(), "true" | "yes" | "on" | "y"),
        None => default_of(key),
    }
}

//------------------------------------------------------------------
// 지금 답변 모델 별칭 — 줄이 없으면 내장 기본 모델
//------------------------------------------------------------------
pub fn read_model(path: Option<&str>) -> String {
    path.and_then(|p| read_value(p, "model")).map(|v| v.to_lowercase()).filter(|v| !v.is_empty())
        .unwrap_or_else(|| crate::models::BASE_ALIAS.to_string())
}

//------------------------------------------------------------------
// 값 저장 — 값만 바꾸고 뒤 주석 칸은 공백으로 맞춘다. 줄이 없으면 generation: 바로 아래에 넣는다
//   돌려줌: Ok(()) / Err(사유)
//------------------------------------------------------------------
pub fn save_value(path: Option<&str>, key: &str, value: &str) -> Result<(), String> {
    let path = path.filter(|p| Path::new(p).is_file()).ok_or("config.yaml 을 찾지 못해 바꾸지 않았습니다")?;
    let raw = std::fs::read_to_string(path).map_err(|e| format!("config.yaml 을 읽지 못했습니다: {}", e))?;
    let nl = if raw.contains("\r\n") { "\r\n" } else { "\n" };
    let bom = raw.starts_with('\u{feff}');
    let mut lines: Vec<String> = raw.trim_start_matches('\u{feff}').split(nl).map(String::from).collect();
    match find(&lines, key) {
        (_, Some(at)) => {
            let (head, old, tail) = split_line(&lines[at], key).unwrap();
            let body = tail.trim_start_matches(' ');
            // 주석 칸이 있던 자리를 지킨다(값이 길어지면 한 칸만 띄운다)
            let gap = if tail.starts_with(' ') {
                " ".repeat((old.len() + tail.len() - body.len()).saturating_sub(value.len()).max(1))
            } else {
                String::new()
            };
            lines[at] = format!("{}{}{}{}", head, value, gap, body);
        }
        (Some(sec), None) => lines.insert(sec + 1, format!("  {}: {}", key, value)),
        (None, None) => return Err("config.yaml 에 generation 구역이 없어 바꾸지 않았습니다".into()),
    }
    let text = format!("{}{}", if bom { "\u{feff}" } else { "" }, lines.join(nl));
    std::fs::write(path, text).map_err(|e| format!("config.yaml 에 쓰지 못했습니다: {}", e))
}

//------------------------------------------------------------------
// 켜기/끄기 저장 (멀티턴) — label = 안내 글의 이름. 돌려줌: Ok(안내 글) / Err(사유)
//------------------------------------------------------------------
pub fn save(path: Option<&str>, on: bool, key: &str, label: &str) -> Result<String, String> {
    save_value(path, key, if on { "true" } else { "false" })?;
    Ok(format!("{}를 {}", label, if on { "켰습니다" } else { "껐습니다" }))
}

//------------------------------------------------------------------
// 0.1.3 정밀 모드에서 옮겨 가기 (모델 받기 설계서 §8)
//=> precision: true 면 앞으로는 설정 > 모델이 정하므로 false 로 적고,
//   Qwen3-4B 가 있으면 model 을 qwen3-4b-q4 로 바꾼다 — 워커는 이 모델에 이웃 청크를 붙이므로 전과 같은 조건으로 답한다.
//   model_present = Qwen3-4B 파일이 있나. 돌려줌: 바꿨으면 Some(안내 글)
//------------------------------------------------------------------
pub fn migrate_precision(path: Option<&str>, model_present: bool) -> Option<String> {
    let p = path?;
    if !read(p, "precision") {
        return None;
    }
    if model_present {
        save_value(Some(p), "model", OLD_PRECISION_ALIAS).ok()?;
    }
    save_value(Some(p), "precision", "false").ok()?;
    Some(if model_present {
        "정밀 모드를 설정 > 모델로 옮겼습니다 — 답변 모델 Qwen3-4B(이웃 청크 포함)".to_string()
    } else {
        "정밀 모드를 껐습니다 — Qwen3-4B 가 없어 원래 모델로 답합니다. 설정 > 모델에서 정밀 모델을 받을 수 있습니다".to_string()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // 켰다 끄면 파일이 글자까지 원래대로 — 주석 칸 정렬·CRLF 보존 (Python 판과 같은 결과)
    #[test]
    fn toggle_keeps_file() {
        let dir = std::env::temp_dir().join("rsb_precision_test");
        let _ = std::fs::create_dir_all(&dir);
        let p = dir.join("config.yaml");
        let orig = "retrieval:\r\n  precision: x   # 다른 구역\r\ngeneration:\r\n  top_k: 3\r\n  precision: false              # 주석\r\n";
        std::fs::write(&p, orig).unwrap();
        let ps = p.to_string_lossy().into_owned();
        assert!(!read(&ps, "precision"));
        save(Some(&ps), true, "precision", "정밀 모드").unwrap();
        assert!(read(&ps, "precision"));
        assert!(std::fs::read_to_string(&p).unwrap().contains("  precision: true               # 주석\r\n"));
        save(Some(&ps), false, "precision", "정밀 모드").unwrap();
        assert_eq!(std::fs::read_to_string(&p).unwrap(), orig);
        // 멀티턴 — 줄이 없으면 기본 켬, 끄면 generation: 아래에 줄이 생긴다
        assert!(read(&ps, "multiturn"));
        save(Some(&ps), false, "multiturn", "멀티턴").unwrap();
        assert!(!read(&ps, "multiturn"));
        assert!(read(&ps, "precision") == false);
    }

    // 모델 줄 바꾸기 · 0.1.3 정밀 모드 옮겨 가기
    #[test]
    fn model_and_migrate() {
        let dir = std::env::temp_dir().join(format!("rsb_cfgline_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let p = dir.join("config.yaml");
        std::fs::write(&p, "generation:\n  model: qwen3-1.7b-q4          # 기본\n  precision: true               # 옛\n").unwrap();
        let ps = p.to_string_lossy().into_owned();
        assert_eq!(read_model(Some(&ps)), "qwen3-1.7b-q4");
        save_value(Some(&ps), "model", "qwen3.5-4b-q4").unwrap();
        assert_eq!(read_model(Some(&ps)), "qwen3.5-4b-q4");
        assert!(std::fs::read_to_string(&p).unwrap().contains("  model: qwen3.5-4b-q4          # 기본\n"));
        // 4B 가 있으면 모델을 qwen3-4b-q4 로, 정밀 모드는 끈다. 두 번째는 할 일 없음
        assert!(migrate_precision(Some(&ps), true).is_some());
        assert_eq!(read_model(Some(&ps)), "qwen3-4b-q4");
        assert!(!read(&ps, "precision"));
        assert!(migrate_precision(Some(&ps), true).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
