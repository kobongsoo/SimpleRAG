//------------------------------------------------------------------
// 동의어 승인 — 후보 읽기 · 사용자 사전(synonyms.yaml) 고치기 (동의어 설계서 4단계 §8, Python synonym_store.py 와 같다)
//=> SimpleRAG 의 suggest-synonyms 가 만든 후보(synonym_candidates.json)를 설정 창에 보여 주고,
//   [맞음]·[아님]·[직접 추가] 한 것을 워커의 데이터 폴더 synonyms.yaml 에 적는다.
//   워커는 이 파일의 수정 시각이 바뀌면 다음 질문 때 다시 읽는다.
//   파일 글은 Python 판과 글자까지 같다 — 낱말은 JSON 꼴 따옴표 글자(YAML 로도 맞다). 직전 파일은 .bak 으로 남긴다.
//------------------------------------------------------------------

use serde_json::{json, Value};

pub const USER_NAME: &str = "synonyms.yaml";
pub const CANDIDATES_NAME: &str = "synonym_candidates.json";
pub const MISSES_NAME: &str = "synonym_misses.jsonl";
// 설정 창 동의어 탭에 한 번에 보여 줄 후보 줄 수 — 승인하면 다음 후보가 올라온다(많으면 보지 않는다, 설계서 §7)
pub const SHOW_ROWS: usize = 5;

const HEADER: &str = "# 내 동의어 사전 — RAGSearchBox 설정 창의 [맞음]·[직접 추가]·[아님] 이 적는다\n\
#   synonyms: 문서 용어: [일상어, ...]   질문에 일상어가 있으면 키워드 검색에 문서 용어를 덧붙인다\n\
#   rejected: [일상어, 문서 용어]        [아님] 한 짝 — 다시 제안하지 않고, 기본 사전에 있어도 쓰지 않는다\n\
#   손으로 고쳐도 되지만 설정 창에서 다시 쓰면 주석은 사라진다(직전 파일은 synonyms.yaml.bak)\n";

// 문서 용어 → 일상어들 (적힌 순서 보존)
pub type Syn = Vec<(String, Vec<String>)>;
// [일상어, 문서 용어] 짝들
pub type Rej = Vec<(String, String)>;

fn path(home: &str, name: &str) -> std::path::PathBuf {
    std::path::Path::new(home).join(name)
}

// YAML 값 → 글자(숫자 등도 글자로)
fn ystr(v: &serde_yaml::Value) -> String {
    match v {
        serde_yaml::Value::String(s) => s.trim().to_string(),
        other => serde_yaml::to_string(other).unwrap_or_default().trim().to_string(),
    }
}

//------------------------------------------------------------------
// 사용자 사전 읽기 — 없으면 빈 사전, 모양이 틀린 항목은 건너뛴다
//   돌려줌: Err = YAML 로 읽히지 않음(덮어써서 지우지 않게 — 호출자가 안내한다)
//------------------------------------------------------------------
pub fn load_user(home: &str) -> Result<(Syn, Rej), String> {
    let p = path(home, USER_NAME);
    if !p.is_file() {
        return Ok((vec![], vec![]));
    }
    let text = std::fs::read_to_string(&p).map_err(|e| format!("synonyms.yaml 을 읽지 못했습니다: {}", e))?;
    let y: serde_yaml::Value = serde_yaml::from_str(&text).map_err(|e| format!("synonyms.yaml 을 읽지 못했습니다: {}", e))?;
    let mut syn: Syn = vec![];
    if let Some(m) = y.get("synonyms").and_then(|v| v.as_mapping()) {
        for (term, words) in m {
            let t = ystr(term);
            let ws: Vec<String> = words.as_sequence().into_iter().flatten().map(ystr).filter(|w| !w.is_empty()).collect();
            if !t.is_empty() && !ws.is_empty() {
                syn.push((t, ws));
            }
        }
    }
    let mut rej: Rej = vec![];
    for pair in y.get("rejected").and_then(|v| v.as_sequence()).into_iter().flatten() {
        if let Some(s) = pair.as_sequence().filter(|s| s.len() == 2) {
            rej.push((ystr(&s[0]), ystr(&s[1])));
        }
    }
    Ok((syn, rej))
}

//------------------------------------------------------------------
// 사전 글 만들기 (Python dump 와 같은 글)
//------------------------------------------------------------------
pub fn dump(syn: &Syn, rej: &Rej) -> String {
    let q = |s: &str| serde_json::to_string(s).unwrap();
    let mut lines = vec![HEADER.to_string(), "synonyms:".to_string()];
    for (t, ws) in syn {
        lines.push(format!("  {}: [{}]", q(t), ws.iter().map(|w| q(w)).collect::<Vec<_>>().join(", ")));
    }
    lines.push("rejected:".to_string());
    for (w, t) in rej {
        lines.push(format!("  - [{}, {}]", q(w), q(t)));
    }
    lines.join("\n") + "\n"
}

//------------------------------------------------------------------
// 사용자 사전 쓰기 — 원래 파일은 .bak, 임시 파일에 다 쓴 뒤 바꿔 넣는다
//------------------------------------------------------------------
pub fn save_user(home: &str, syn: &Syn, rej: &Rej) -> Result<(), String> {
    let p = path(home, USER_NAME);
    if p.is_file() {
        std::fs::copy(&p, path(home, &format!("{}.bak", USER_NAME))).map_err(|e| e.to_string())?;
    }
    let tmp = path(home, &format!("{}.tmp", USER_NAME));
    std::fs::write(&tmp, dump(syn, rej)).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &p).map_err(|e| e.to_string())
}

// 이 일상어를 이미 사전에 넣었나
fn approved(syn: &Syn, word: &str) -> bool {
    syn.iter().any(|(_, ws)| ws.iter().any(|w| w == word))
}

//------------------------------------------------------------------
// 보여 줄 후보 줄 — 아직 승인하지 않았고 [아님] 하지 않은 문서 용어가 남은 낱말만
//   돌려줌: (줄 목록 [{word, terms, example, docs:{용어: [문서]}}], 남은 후보 전체 수)
//------------------------------------------------------------------
pub fn pending(home: &str, limit: usize) -> (Vec<Value>, usize) {
    let cands: Vec<Value> = std::fs::read(path(home, CANDIDATES_NAME)).ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| v.get("candidates").and_then(|c| c.as_array()).cloned())
        .unwrap_or_default();
    let (syn, rej) = load_user(home).unwrap_or_default();
    let mut rows = vec![];
    for c in &cands {
        let w = c["word"].as_str().unwrap_or("");
        if w.is_empty() || approved(&syn, w) {
            continue;
        }
        let terms: Vec<&Value> = c["terms"].as_array().into_iter().flatten()
            .filter(|t| t["term"].as_str().map(|s| !s.is_empty() && !rej.iter().any(|(rw, rt)| rw == w && rt == s)).unwrap_or(false))
            .collect();
        if terms.is_empty() {
            continue;
        }
        let mut docs = serde_json::Map::new();
        for t in &terms {
            docs.insert(t["term"].as_str().unwrap().to_string(), t.get("docs").cloned().unwrap_or(json!([])));
        }
        rows.push(json!({"word": w, "terms": terms.iter().map(|t| t["term"].clone()).collect::<Vec<_>>(),
                         "example": c["examples"].get(0).and_then(|e| e.as_str()).unwrap_or(""), "docs": docs}));
    }
    let n = rows.len();
    rows.truncate(limit);
    (rows, n)
}

//------------------------------------------------------------------
// 짝 확인 — 빈 칸 · 한 글자 일상어 · 같은 말은 받지 않는다 (Python check_pair 와 같은 글)
//------------------------------------------------------------------
pub fn check_pair(word: &str, term: &str) -> Option<String> {
    if word.is_empty() || term.is_empty() {
        return Some("일상어와 문서 용어를 모두 적으세요".into());
    }
    let nw = word.replace(' ', "");
    if nw.chars().count() < 2 {
        return Some("한 글자 일상어는 다른 낱말에도 걸려 넣지 않습니다(예: \"애\" → \"애플\")".into());
    }
    if nw == term.replace(' ', "") {
        return Some("일상어와 문서 용어가 같습니다".into());
    }
    None
}

//------------------------------------------------------------------
// [맞음] · [직접 추가] — 일상어 → 문서 용어 를 사전에 넣는다(같은 짝이 [아님] 에 있으면 거기서 뺀다)
//   돌려줌: (ok, 안내 글)
//------------------------------------------------------------------
pub fn approve(home: &str, word: &str, term: &str) -> (bool, String) {
    let (word, term) = (word.trim(), term.trim());
    if let Some(bad) = check_pair(word, term) {
        return (false, bad);
    }
    let r = (|| -> Result<bool, String> {
        let (mut syn, mut rej) = load_user(home)?;
        match syn.iter_mut().find(|(t, _)| t == term) {
            Some((_, ws)) if ws.iter().any(|w| w == word) => return Ok(false),
            Some((_, ws)) => ws.push(word.to_string()),
            None => syn.push((term.to_string(), vec![word.to_string()])),
        }
        rej.retain(|(w, t)| !(w == word && t == term));
        save_user(home, &syn, &rej)?;
        Ok(true)
    })();
    match r {
        Ok(true) => (true, format!("넣었습니다: {} → {} (다음 질문부터)", word, term)),
        Ok(false) => (true, format!("이미 들어 있습니다: {} → {}", word, term)),
        Err(e) => (false, format!("사전에 적지 못했습니다: {}", e)),
    }
}

//------------------------------------------------------------------
// [아님] — 이 짝은 다시 제안하지 않는다(앞서 승인했던 짝이면 사전에서도 뺀다)
//   돌려줌: (ok, 안내 글)
//------------------------------------------------------------------
pub fn reject(home: &str, word: &str, term: &str) -> (bool, String) {
    let (word, term) = (word.trim(), term.trim());
    if word.is_empty() || term.is_empty() {
        return (false, "고른 짝이 없습니다".into());
    }
    let r = (|| -> Result<(), String> {
        let (mut syn, mut rej) = load_user(home)?;
        if !rej.iter().any(|(w, t)| w == word && t == term) {
            rej.push((word.to_string(), term.to_string()));
        }
        for (t, ws) in syn.iter_mut() {
            if t == term {
                ws.retain(|w| w != word);
            }
        }
        syn.retain(|(_, ws)| !ws.is_empty());
        save_user(home, &syn, &rej)
    })();
    match r {
        Ok(()) => (true, format!("다시 묻지 않습니다: {} → {}", word, term)),
        Err(e) => (false, format!("사전에 적지 못했습니다: {}", e)),
    }
}

//------------------------------------------------------------------
// 승인한 짝 목록 — [(일상어, 문서 용어)] 사전에 적힌 순서 (못 읽으면 빈 목록)
//------------------------------------------------------------------
pub fn approved_pairs(home: &str) -> Vec<(String, String)> {
    load_user(home).map(|(syn, _)| syn.into_iter().flat_map(|(t, ws)| ws.into_iter().map(move |w| (w, t.clone()))).collect())
        .unwrap_or_default()
}

//------------------------------------------------------------------
// [빼기] — 승인한 짝을 사전에서 뺀다([아님] 과 달리 다시 제안될 수 있다) (Python remove 와 같은 글)
//------------------------------------------------------------------
pub fn remove(home: &str, word: &str, term: &str) -> (bool, String) {
    let r = (|| -> Result<bool, String> {
        let (mut syn, rej) = load_user(home)?;
        let found = syn.iter().any(|(t, ws)| t == term && ws.iter().any(|w| w == word));
        if !found {
            return Ok(false);
        }
        for (t, ws) in syn.iter_mut() {
            if t == term {
                ws.retain(|w| w != word);
            }
        }
        syn.retain(|(_, ws)| !ws.is_empty());
        save_user(home, &syn, &rej)?;
        Ok(true)
    })();
    match r {
        Ok(true) => (true, format!("뺐습니다: {} → {} (다음 질문부터)", word, term)),
        Ok(false) => (false, format!("사전에 없는 짝입니다: {} → {}", word, term)),
        Err(e) => (false, format!("사전에 적지 못했습니다: {}", e)),
    }
}

//------------------------------------------------------------------
// 안내 줄에 쓸 수 — (놓친 질문 수(같은 질문은 하나로), 승인한 일상어 수)
//------------------------------------------------------------------
pub fn counts(home: &str) -> (usize, usize) {
    let mut qs = std::collections::HashSet::new();
    if let Ok(text) = std::fs::read_to_string(path(home, MISSES_NAME)) {
        for line in text.lines() {
            if let Some(q) = serde_json::from_str::<Value>(line).ok().and_then(|v| v["q"].as_str().map(|s| s.to_string())) {
                if !q.is_empty() {
                    qs.insert(q);
                }
            }
        }
    }
    let n_ok = load_user(home).map(|(syn, _)| syn.iter().map(|(_, ws)| ws.len()).sum()).unwrap_or(0);
    (qs.len(), n_ok)
}

//------------------------------------------------------------------
// 동의어 칸 안내 줄 (Python info_window.synonym_note 와 같은 글)
//------------------------------------------------------------------
pub fn note(n_miss: usize, n_left: usize, n_ok: usize, msg: &str) -> String {
    let head = if n_left > 0 {
        format!("후보 {}개 남음 · 승인한 동의어 {}개", n_left, n_ok)
    } else if n_miss > 0 {
        format!("답을 못 찾은 질문 {}개 — [후보 만들기] 로 사전 후보를 뽑으세요 · 승인한 동의어 {}개", n_miss, n_ok)
    } else {
        format!("답을 못 찾은 질문이 쌓이면 여기서 동의어 후보를 고를 수 있습니다 · 승인한 동의어 {}개", n_ok)
    };
    if msg.is_empty() { head } else { format!("{}\n{}", head, msg) }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Python test_synonym_store 와 같은 흐름 — 같은 파일 글이 나오는지까지
    #[test]
    fn approve_reject_like_python() {
        let dir = std::env::temp_dir().join(format!("rsb_syn_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let home = dir.to_string_lossy().into_owned();
        std::fs::write(dir.join(CANDIDATES_NAME), r#"{"candidates":[
            {"word":"할아버지","examples":["할아버지 돌아가시면"],"terms":[{"term":"조부모","docs":["취업규칙.pdf"]},{"term":"부모","docs":[]}]},
            {"word":"와이프","examples":["와이프 출산"],"terms":[{"term":"여비","docs":[]},{"term":"배우자","docs":[]}]}]}"#).unwrap();
        assert_eq!(pending(&home, 5).1, 2);
        assert!(approve(&home, "할아버지", "조부모").0);
        assert!(approve(&home, "외할머니", "조부모").0);
        assert!(reject(&home, "와이프", "여비").0);
        let (rows, n) = pending(&home, 5);
        assert_eq!((n, rows[0]["terms"].clone()), (1, json!(["배우자"])));
        let text = std::fs::read_to_string(dir.join(USER_NAME)).unwrap();
        assert!(text.contains("synonyms:\n  \"조부모\": [\"할아버지\", \"외할머니\"]\nrejected:\n  - [\"와이프\", \"여비\"]\n"), "{}", text);
        assert!(!approve(&home, "애", "자녀").0);
        // 마음이 바뀌면 — 승인했던 짝을 [아님]
        assert!(reject(&home, "할아버지", "조부모").0);
        assert_eq!(load_user(&home).unwrap().0, vec![("조부모".to_string(), vec!["외할머니".to_string()])]);
        assert_eq!(approved_pairs(&home), vec![("외할머니".to_string(), "조부모".to_string())]);
        // [빼기] — 사전에서만 빠지고 [아님] 목록은 그대로
        assert!(remove(&home, "외할머니", "조부모").0);
        assert!(!remove(&home, "외할머니", "조부모").0);
        assert!(approved_pairs(&home).is_empty() && load_user(&home).unwrap().1.len() == 2);
        assert!(note(3, 2, 1, "").starts_with("후보 2개 남음"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
