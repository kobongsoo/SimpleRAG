//------------------------------------------------------------------
// 정밀 모드 켜고 끄기 — SimpleRAG config.yaml 의 generation.precision 한 줄만 바꾼다 (Python RAGSearchBox/precision.py 와 같다)
//=> 정밀 모드 = Qwen3-4B 모델 + 근거에 앞뒤 이웃 청크(코드수정 설계서 §7 ④). 골든 100문항 정답 50 → 59,
//   대신 첫 글자(TTFT) 약 3배·답 완료 약 2배 늦다. 워커는 시작할 때 config.yaml 을 읽으므로 바꾼 뒤 모델을 다시 올린다.
//   yaml 로 다시 쓰면 설명 주석이 날아가므로 generation 구역의 precision 줄 값만 갈아 끼운다(줄 끝 CRLF/LF 보존).
//   같은 방식으로 멀티턴(generation.multiturn, 기본 켬)도 켜고 끈다 — read/save 의 key 로 고른다.
//------------------------------------------------------------------

use std::path::Path;

pub const MODEL_FILE: &str = "Qwen3-4B-Q4_K_M.gguf";
// 설정 창에 늘 보이는 주의 글
// 멀티턴 안내 글 (Python precision.MULTITURN_NOTE 와 같다)
pub const MULTITURN_NOTE: &str = "따라 묻는 질문(\"침구류는?\")의 빠진 주제를 같은 대화의 바로 앞 질문에서 채워 찾습니다. 새 대화(✎)를 누르면 끊깁니다. 바꾸면 모델을 다시 올립니다.";

// 키의 기본값 — precision 끔 · multiturn 켬
fn default_of(key: &str) -> bool {
    key == "multiturn"
}

pub const WARN: &str = "⚠️ 켜면 느려집니다 — 첫 글자(TTFT)가 2배 이상(약 2.2 → 7.1초), 답 완료가 약 2배(7.6 → 14.7초). 바꾸면 모델을 다시 올립니다.";

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
// 지금 켜져 있나 (정밀 모드 · 멀티턴) — 줄이 없거나 못 읽으면 그 키의 기본값
//------------------------------------------------------------------
pub fn read(path: &str, key: &str) -> bool {
    let Ok(raw) = std::fs::read_to_string(path) else { return default_of(key) };
    let lines: Vec<String> = raw.trim_start_matches('\u{feff}').lines().map(String::from).collect();
    match find(&lines, key).1 {
        Some(at) => matches!(split_line(&lines[at], key).unwrap().1.to_lowercase().as_str(), "true" | "yes" | "on" | "y"),
        None => default_of(key),
    }
}

//------------------------------------------------------------------
// 켜기/끄기 저장 (정밀 모드 · 멀티턴) — 값만 바꾸고 뒤 주석 칸은 공백으로 맞춘다. 줄이 없으면 generation: 바로 아래에 넣는다
//   label = 안내 글의 이름. 돌려줌: Ok(안내 글) / Err(사유)
//------------------------------------------------------------------
pub fn save(path: Option<&str>, on: bool, key: &str, label: &str) -> Result<String, String> {
    let path = path.filter(|p| Path::new(p).is_file()).ok_or("config.yaml 을 찾지 못해 바꾸지 않았습니다")?;
    let raw = std::fs::read_to_string(path).map_err(|e| format!("config.yaml 을 읽지 못했습니다: {}", e))?;
    let nl = if raw.contains("\r\n") { "\r\n" } else { "\n" };
    let bom = raw.starts_with('\u{feff}');
    let mut lines: Vec<String> = raw.trim_start_matches('\u{feff}').split(nl).map(String::from).collect();
    let value = if on { "true" } else { "false" };
    match find(&lines, key) {
        (_, Some(at)) => {
            let (head, old, tail) = split_line(&lines[at], key).unwrap();
            let body = tail.trim_start_matches(' ');
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
    std::fs::write(path, text).map_err(|e| format!("config.yaml 에 쓰지 못했습니다: {}", e))?;
    Ok(format!("{}를 {}", label, if on { "켰습니다" } else { "껐습니다" }))
}

//------------------------------------------------------------------
// 4B 모델 파일이 있나 — SIMPLERAG_MODELS_DIR → 데이터 폴더 models → 프로그램 폴더 models → 옆 Python 배포본 models
//------------------------------------------------------------------
pub fn model_present(data_home: Option<&str>, app_dir: Option<&str>) -> bool {
    let mut dirs: Vec<std::path::PathBuf> = vec![];
    if let Ok(d) = std::env::var("SIMPLERAG_MODELS_DIR") {
        dirs.push(d.into());
    }
    for d in [data_home, app_dir].into_iter().flatten() {
        dirs.push(Path::new(d).join("models"));
    }
    if let Some(up) = app_dir.and_then(|a| Path::new(a).parent()) {
        dirs.push(up.join("simplerag").join("models"));
        dirs.push(up.join("dist").join("simplerag").join("models"));
    }
    dirs.iter().any(|d| d.join(MODEL_FILE).is_file())
}

//------------------------------------------------------------------
// 설정 창 정밀 모드 안내 줄
//------------------------------------------------------------------
pub fn note(present: bool, found: bool, msg: &str) -> String {
    let mut parts = vec![WARN.to_string()];
    if !found {
        parts.push("config.yaml 을 찾지 못해 바꿀 수 없습니다.".into());
    } else if !present {
        parts.push(format!("models 폴더에 {} 가 없어 켜도 기존 모델로 답합니다.", MODEL_FILE));
    }
    if !msg.is_empty() {
        parts.push(msg.to_string());
    }
    parts.join("\n")
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
}
